from collections import OrderedDict

import torch
from torch import nn
import torch.nn.functional as F
from torch.nn.utils import clip_grad_norm_
from sklearn.metrics import accuracy_score, precision_recall_fscore_support, f1_score
from tqdm import tqdm
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime

from baselines import SarcasmClassifier


# author_feature_shape should be (# authors (0 for UNK) x embed_size) if using embeddings, (# features) otherwise
class SarcasmRNN(nn.Module):
    def __init__(self, pretrained_weights, device, ancestor_rnn=False,
                 author_feature_shape=None, subreddit_feature_shape=None, embed_addressee=False,
                 hidden_dim=300, dropout=0.5, freeze_embeddings=True,
                 num_rnn_layers=1, second_linear_layer=False, rnn_cell='GRU', attn_size=None):

        super(SarcasmRNN, self).__init__()

        self.hidden_dim = hidden_dim
        self.num_rnn_layers = num_rnn_layers
        self.rnn_cell = rnn_cell
        self.ancestor_rnn = ancestor_rnn
        self.device = device
        self.embed_addressee = embed_addressee
        self.attn_size = attn_size

        self.norm_penalized_params = []


        self.author_feature_shape = author_feature_shape
        if self.author_feature_shape is None:
            self.author_dims = 0
        else:
            self.author_dims = self.author_feature_shape[-1]
            if embed_addressee: self.author_dims = self.author_dims * 2
            if len(self.author_feature_shape) == 2:
                self.author_embeddings = nn.Embedding(*self.author_feature_shape)

        self.subreddit_feature_shape = subreddit_feature_shape
        if self.subreddit_feature_shape is None:
            self.subreddit_dims = 0
        else:
            self.subreddit_dims = self.subreddit_feature_shape[-1]
            self.subreddit_embeddings = nn.Embedding(*self.subreddit_feature_shape)


        # Word vectors
        embedding_dim = pretrained_weights.shape[1]
        self.embeddings = nn.Embedding.from_pretrained(pretrained_weights, freeze=freeze_embeddings)

        rnn_hidden_shape = num_rnn_layers, 1, hidden_dim

        self.rnns = []
        if self.ancestor_rnn:
            self.rnn_a = {'rnn_name' : 'rnn_a_'}
            self.rnns.append(self.rnn_a)
        self.rnn_r = {'rnn_name' : 'rnn_r_'}
        self.rnns.append(self.rnn_r)
        self.num_rnns = 2 if self.ancestor_rnn else 1

        for rnn in self.rnns:
            rn = rnn['rnn_name']
            setattr(self, rn + 'rnn_f', getattr(nn, rnn_cell)(embedding_dim, hidden_dim, num_layers=num_rnn_layers,
                dropout=dropout if num_rnn_layers > 1 else 0, batch_first=True))
            rnn['rnn_f'] = getattr(self, rn + 'rnn_f')

            setattr(self, rn + 'rnn_b', getattr(nn, rnn_cell)(embedding_dim, hidden_dim, num_layers=num_rnn_layers,
                                   dropout=dropout if num_rnn_layers > 1 else 0, batch_first=True))
            rnn['rnn_b'] = getattr(self, rn + 'rnn_b')

            if self.attn_size is not None:
                setattr(self, rn + 'W_omega',
                        nn.Parameter(torch.randn(2*self.hidden_dim, self.attn_size).to(device), requires_grad=True))
                rnn['W_omega'] = getattr(self, rn + 'W_omega')

                setattr(self, rn + 'b_omega',
                        nn.Parameter(torch.randn(1, self.attn_size).to(device), requires_grad=True))
                rnn['b_omega'] = getattr(self, rn + 'b_omega')

                setattr(self, rn + 'u_omega',
                        nn.Parameter(torch.randn(self.attn_size, 1).to(device), requires_grad=True))
                rnn['u_omega'] = getattr(self, rn + 'u_omega')

                self.norm_penalized_params += [rnn['W_omega'], rnn['b_omega'], rnn['u_omega']]

        self.dropout_op = nn.Dropout(dropout)


        # Switch between going straight from RNN state to output or
        # putting in an intermediate relu->hidden layer (halving the size)
        self.second_linear_layer = second_linear_layer
        if self.second_linear_layer:
            # Taken in features from 2 directions of 1 or 2 RNNs, author embedding, and subreddit embedding
            self.linear1 = nn.Linear(hidden_dim*2*self.num_rnns + self.author_dims + self.subreddit_dims, hidden_dim)
            self.relu = nn.ReLU()
            self.linear2 = nn.Linear(hidden_dim, 1)
            self.norm_penalized_params += [self.linear1.weight, self.linear2.weight]
        else:
            self.linear = nn.Linear(hidden_dim*2*self.num_rnns + self.author_dims + self.subreddit_dims, 1)
            self.norm_penalized_params += [self.linear.weight]

    def penalized_l2_norm(self):
        l2_reg = None
        for param in self.norm_penalized_params:
            if l2_reg is None:
                l2_reg = param.norm(2)
            else:
                l2_reg = l2_reg + param.norm(2)
        return l2_reg

    # inputs should be B x max_len LongTensor, lengths should be B-length 1D LongTensor
    def forward(self, inputs, inputs_reversed, lengths, author_features=None, subreddit_features=None, **kwargs):
        if self.author_feature_shape is not None and author_features is None:
            raise ValueError("Need author features for forward")
        if self.subreddit_feature_shape is not None and subreddit_features is None:
            raise ValueError("Need subreddit features for forward")

        batch_size = inputs.shape[0]

        if self.ancestor_rnn:
            assert len(inputs.shape) == 3 #batch_size, 2, max_len
            max_len = inputs.shape[2]
            self.rnn_a['embedded_inputs'] = self.embeddings(inputs[:, 0, :].squeeze())
            self.rnn_a['embedded_inputs_reversed'] = self.embeddings(inputs_reversed[:, 0, :].squeeze())
            self.rnn_a['lengths'] = lengths[:, 0].squeeze()

            self.rnn_r['embedded_inputs'] = self.embeddings(inputs[:, 1, :].squeeze())
            self.rnn_r['embedded_inputs_reversed'] = self.embeddings(inputs_reversed[:, 1, :].squeeze())
            self.rnn_r['lengths'] = lengths[:, 1].squeeze()
        else:
            assert len(inputs.shape) == 2 #batch_size, max_len
            max_len = inputs.shape[1]
            self.rnn_r['embedded_inputs'] = self.embeddings(inputs)
            self.rnn_r['embedded_inputs_reversed'] = self.embeddings(inputs_reversed)
            self.rnn_r['lengths'] = lengths


        for rnn in self.rnns:

            rnn_states_f, _ = rnn['rnn_f'](rnn['embedded_inputs'])
            rnn_states_b, _ = rnn['rnn_b'](rnn['embedded_inputs_reversed'])

            if self.attn_size is None:
                #Take final states of RNN
                idx = torch.ones((batch_size, 1, self.hidden_dim), dtype=torch.long).to(self.device) * \
                      (rnn['lengths'] - 1).view(-1, 1, 1)
                final_states_f = torch.gather(rnn_states_f, 1, idx).squeeze()
                final_states_b = torch.gather(rnn_states_b, 1, idx).squeeze()
                rnn['final_states'] = torch.cat((final_states_f, final_states_b), 1)
            else:
                #Apply attention!
                zeroed_states_f = None
                reversed_states_b = None
                for i in range(batch_size):
                    l = int(rnn['lengths'][i])

                    # Zero out places where the RNN ran over the end of the sequence:
                    forward_indices = torch.LongTensor([j for j in range(l)]).to(self.device)
                    shortened_tensor = torch.index_select(rnn_states_f[i], 0, forward_indices)
                    padding = torch.zeros((max_len - l, self.hidden_dim), dtype=torch.float).to(self.device)
                    shortened_tensor = torch.cat((shortened_tensor, padding),0).unsqueeze(0)
                    if zeroed_states_f is None: zeroed_states_f = shortened_tensor
                    else: zeroed_states_f = torch.cat((zeroed_states_f, shortened_tensor),0)

                    # Flip every reverse-RNN set of outputs in the batch, zero it out too
                    reversed_indices = torch.LongTensor([j for j in range(l - 1, -1, -1)]).to(self.device)
                    inverted_tensor = torch.index_select(rnn_states_b[i], 0, reversed_indices)
                    inverted_tensor = torch.cat((inverted_tensor, padding),0).unsqueeze(0)
                    if reversed_states_b is None: reversed_states_b = inverted_tensor
                    else: reversed_states_b = torch.cat((reversed_states_b, inverted_tensor),0)

                rnn_states = torch.cat((rnn_states_f, reversed_states_b), 2)
                u = torch.tanh(torch.matmul(rnn_states, rnn['W_omega']) + rnn['b_omega'])
                alpha = F.softmax(torch.matmul(u, rnn['u_omega']), 1)
                rnn['final_states'] = torch.sum(alpha * rnn_states, 1)

        if self.ancestor_rnn:
            final_states = torch.cat((self.rnn_a['final_states'], self.rnn_r['final_states']), 1)
        else:
            final_states = self.rnn_r['final_states']


        dropped_out = self.dropout_op(final_states)

        if author_features is not None:
            if len(self.author_feature_shape) == 2:
                if self.embed_addressee:
                    addressee_indices = author_features[:, 0]
                    author_indices = author_features[:, 1]
                    addresse_x = self.author_embeddings(addressee_indices)
                    author_only_x = self.author_embeddings(author_indices)
                    author_x = torch.cat((addresse_x, author_only_x), 1)
                else:
                    author_x = self.author_embeddings(author_features)
            else:
                if self.embed_addressee:
                    addresee_priors = author_features[:, 0]
                    author_priors = author_features[:, 1]
                    author_x = torch.cat((addresee_priors, author_priors), 1)
                else:
                    author_x = author_features
            dropped_out = torch.cat((dropped_out, author_x), 1)

        if subreddit_features is not None:
            subreddit_x = self.subreddit_embeddings(subreddit_features)
            dropped_out = torch.cat((dropped_out, subreddit_x), 1)

        if self.second_linear_layer:
            x = self.linear1(dropped_out)
            x = self.relu(self.dropout_op(x))
            post_linear = self.linear2(x)
        else:
            post_linear = self.linear(dropped_out)

        probs = F.sigmoid(post_linear).squeeze()  # sigmoid output for binary classification
        return probs

    def predict(self, inputs, inputs_reversed, lengths, author_features=None, subreddit_features=None):
        sigmoids = self(inputs, inputs_reversed, lengths, author_features, subreddit_features)
        return torch.round(sigmoids)


# epochs_to_persist: how many epochs of non-increasing train/val score to go for
# Currently hard coded with Adam optimizer and BCE loss
class NNClassifier(SarcasmClassifier):
    def __init__(self, batch_size, max_epochs, epochs_to_persist, early_stopping,
                 verbose, progress_bar, output_graphs,
                 balanced_setting, recall_multiplier,
                 l2_lambda, lr, author_feature_shape, subreddit_feature_shape,
                 device, Module, module_args):

        self.model = Module(device=device, author_feature_shape=author_feature_shape,
                            subreddit_feature_shape=subreddit_feature_shape, **module_args).to(device)

        self.batch_size = batch_size
        self.max_epochs = max_epochs
        self.epochs_to_persist = epochs_to_persist
        self.early_stopping = early_stopping # Stop when val score stops improving?
        self.verbose = verbose
        self.progress_bar = progress_bar
        self.output_graphs = output_graphs
        self.balanced_setting = balanced_setting
        self.recall_multiplier = recall_multiplier
        self.l2_lambda = l2_lambda
        self.lr = lr
        self.author_feature_shape = author_feature_shape
        self.subreddit_feature_shape = subreddit_feature_shape
        self.penalize_rnn_weights = False

    # train_data should have X, Y, lengths, author_features, subreddit_features
    # val_datas should be dictionary indexed by name of val set, with value of each being
    # dict with same features as X
    def fit(self, train_data, val_data, holdout_datas):

        if self.author_feature_shape is not None and train_data['author_features'] is None:
            raise ValueError("Need author features to fit")

        if self.subreddit_feature_shape is not None and train_data['subreddit_features'] is None:
            raise ValueError("Need author features to fit")

        n = len(train_data['X'])
        assert n == len(train_data['Y']) == len(train_data['lengths'])
        if self.author_feature_shape is not None: assert n == len(train_data['author_features'])
        if self.subreddit_feature_shape is not None: assert n == len(train_data['subreddit_features'])

        criterion = nn.BCELoss(reduce=False)
        trainable_params = filter(lambda p: p.requires_grad, self.model.parameters())
        optimizer = torch.optim.Adam(trainable_params, lr=self.lr,
                                     weight_decay=self.l2_lambda if self.penalize_rnn_weights else 0)

        num_train_batches = n // self.batch_size + 1

        train_losses = []
        train_f1s = []
        val_f1s = []
        best_model_state = None
        best_model_epoch = None

        epoch_iter = tqdm(range(self.max_epochs)) if self.progress_bar and not self.verbose \
            else range(self.max_epochs)
        for epoch in epoch_iter:
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            if self.verbose: print("\n\nStarting to train on epoch {} at time {}".format(epoch, timestamp), flush=True)
            self.model.train()

            shuffle_indices = torch.randperm(n)
            for k in train_data.keys():
                if train_data[k] is not None: train_data[k] = train_data[k][shuffle_indices]

            running_loss = 0.0
            batch_train_f1s = []
            for b in (tqdm(range(num_train_batches)) if self.progress_bar and self.verbose
                      else range(num_train_batches)):

                s, e = b*self.batch_size, (b+1)*self.batch_size
                batch = {k : (v[s:e] if v is not None else None) for k,v in train_data.items()}

                optimizer.zero_grad()
                outputs = self.model(batch['X'], batch['X_reversed'], batch['lengths'],
                                     batch['author_features'], batch['subreddit_features'])
                batch_train_f1s.append(f1_score(batch['Y'].detach(), torch.round(outputs.detach())))
                loss = criterion(outputs, batch['Y'])
                if not self.balanced_setting:
                    assert self.recall_multiplier is not None
                    loss = loss * ((batch['Y'] == 1).float() * self.recall_multiplier + 1)
                loss = torch.mean(loss)
                if self.l2_lambda and not self.penalize_rnn_weights:
                    loss += self.model.penalized_l2_norm() * self.l2_lambda
                loss.backward()
                clip_grad_norm_(trainable_params, 0.5)
                optimizer.step()
                running_loss += loss.item()

            train_losses.append(running_loss/num_train_batches)
            train_f1s.append(np.mean(batch_train_f1s))

            val_predictions = self.predict(val_data['X'], val_data['X_reversed'], val_data['lengths'],
                                           val_data['author_features'], val_data['subreddit_features'])
            rate_val_correct = accuracy_score(val_data['Y'].detach(), val_predictions.detach())
            precision, recall, f1, support =  precision_recall_fscore_support(
                val_data['Y'].detach(), val_predictions.detach())
            mean_f1 = np.mean(f1)
            val_f1s.append(mean_f1)

            # If this is the best val score we've seen, save the model weights
            if np.argmax(val_f1s) == (len(val_f1s) - 1):
                best_model_state = self.model.state_dict()
                best_model_epoch = epoch

            if self.verbose:
                print("\nAvg Loss: {}. Train (unpaired!) F1: {} ".format(
                    train_losses[-1], train_f1s[-1]), flush=True)
                print("On val set - Accuracy: {}. Precision: {}. Recall: {}. F1: {} (Mean {}).".format(
                    rate_val_correct, precision, recall, f1, mean_f1), flush=True)

            if self.early_stopping and epoch - np.argmax(val_f1s) >= self.epochs_to_persist: break
            if epoch - np.argmin(train_losses) >= self.epochs_to_persist: break

        print("\n\nTraining complete. Best (unpaired) train F1 {} from epoch {}".format(
            np.max(train_f1s), np.argmax(train_f1s)), flush=True)
        print("Best val F1 {} from epoch {}".format(
            np.max(val_f1s), np.argmax(val_f1s)), flush=True)

        print("Loading best model, which was from epoch {}".format(best_model_epoch))
        self.model.load_state_dict(best_model_state)

        holdout_results = OrderedDict()
        for holdout_label, holdout_data in holdout_datas.items():
            holdout_predictions = self.predict(holdout_data['X'], holdout_data['X_reversed'],
                holdout_data['lengths'], holdout_data['author_features'], holdout_data['subreddit_features'])
            rate_holdout_correct = accuracy_score(holdout_data['Y'].detach(), holdout_predictions.detach())
            precision, recall, f1, support =  precision_recall_fscore_support(
                holdout_data['Y'].detach(), holdout_predictions.detach())
            mean_holdout_f1 = np.mean(f1)
            holdout_results[holdout_label] = rate_holdout_correct, precision, recall, f1, support, mean_holdout_f1
            print("On holdout set '{}' - Accuracy: {}. Precision: {}. Recall: {}. F1: {} (Mean {}).".format(
                holdout_label, rate_holdout_correct, precision, recall, f1, mean_holdout_f1), flush=True)

        if self.output_graphs: self.make_graphs(train_losses, train_f1s, val_f1s)

        primary_holdout_f1 = list(holdout_results.values())[0][3]
        return primary_holdout_f1, train_losses, train_f1s, val_f1s, holdout_results

    def predict(self, X, X_reversed, lengths, author_features=None, subreddit_features=None):
        self.model.eval()
        with torch.no_grad():
            predictions = None
            n = len(X)
            num_batches = n // self.batch_size + 1
            for b in range(num_batches):
                s, e = b*self.batch_size, (b+1)*self.batch_size
                X_batch, X_reversed_batch, lengths_batch = X[s:e], X_reversed[s:e], lengths[s:e]
                authors_batch = author_features[s:e] if author_features is not None else None
                subreddits_batch = subreddit_features[s:e] if subreddit_features is not None else None

                if self.balanced_setting:
                    cur_predictions = self.predict_balanced(X_batch, X_reversed_batch,
                                                            lengths_batch, authors_batch, subreddits_batch)
                else:
                    cur_predictions = self.model.predict(X_batch, X_reversed_batch,
                                                         lengths_batch, authors_batch, subreddits_batch)

                if predictions is None: predictions = cur_predictions
                else: predictions = torch.cat((predictions, cur_predictions), 0)
        return predictions

    # In the balanced case, we know that exactly one of every pair of comments is sarcastic
    def predict_balanced(self, X, X_reversed, lengths, author_features=None, subreddit_features=None):
        probs = self.model(X, X_reversed, lengths, author_features, subreddit_features)
        assert len(probs) % 2 == 0
        n = len(probs) // 2
        predictions = torch.zeros(2*n)
        for i in range(n):
            if probs[2*i] > probs[2*i + 1]: predictions[2*i] = 1
            else: predictions[2*i + 1] = 1
        return predictions

    def make_graphs(self, train_losses, train_f1s, val_f1s):
        plt.clf()
        plt.plot(train_losses, label='Train loss')
        plt.plot(train_f1s, label='Train F1 (unpaired!)')
        plt.plot(val_f1s, label='Val F1')
        plt.legend()
        plt.xlabel('Epoch')
        plt.ylabel('Score')
        plt.title('Training curves')
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        plt.savefig(timestamp + '.png', bbox_inches='tight')
        plt.close()

