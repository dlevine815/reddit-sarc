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
    def __init__(self, pretrained_weights, device,
                 author_feature_shape=None, subreddit_feature_shape=None,
                 hidden_dim=300, dropout=0.5, freeze_embeddings=True,
                 num_rnn_layers=1, second_linear_layer=False, rnn_cell='GRU'):

        super(SarcasmRNN, self).__init__()

        self.num_rnn_layers = num_rnn_layers
        self.rnn_cell = rnn_cell
        self.device = device

        self.norm_penalized_params = []


        self.author_feature_shape = author_feature_shape
        if self.author_feature_shape is None:
            self.author_dims = 0
        else:
            self.author_dims = self.author_feature_shape[-1]
            if len(self.author_feature_shape) == 2:
                self.author_embeddings = nn.Embedding(*self.author_feature_shape)

        self.subreddit_feature_shape = subreddit_feature_shape
        if self.subreddit_feature_shape is None:
            self.subreddit_dims = 0
        else:
            self.subreddit_dims = self.subreddit_feature_shape[-1]
            self.subreddit_embeddings = nn.Embedding(*self.subreddit_feature_shape)


        embedding_dim = pretrained_weights.shape[1]
        self.embeddings = nn.Embedding.from_pretrained(pretrained_weights, freeze=freeze_embeddings)

        #self.word_lstm_init_h = Parameter(torch.randn(2, 20, word_lstm_dim).type(FloatTensor), requires_grad=True)
        rnn_hidden_shape = 2*num_rnn_layers, 1, hidden_dim
        self.rnn_init_h = nn.Parameter(torch.randn(*rnn_hidden_shape, dtype=torch.float).to(device), requires_grad=True)
        if rnn_cell == 'LSTM':
            self.rnn_init_c = nn.Parameter(torch.randn(*rnn_hidden_shape, dtype=torch.float).to(device), requires_grad=True)

        self.rnn = getattr(nn, rnn_cell)(embedding_dim, hidden_dim, num_layers=num_rnn_layers,
                dropout=dropout if num_rnn_layers > 1 else 0, bidirectional=True, batch_first=True)

        self.dropout = nn.Dropout(dropout)


        # Switch between going straight from RNN state to output or
        # putting in an intermediate relu->hidden layer (halving the size)
        self.second_linear_layer = second_linear_layer
        if self.second_linear_layer:
            self.linear1 = nn.Linear(hidden_dim*2 + self.author_dims + self.subreddit_dims, hidden_dim)
            self.relu = nn.ReLU()
            self.linear2 = nn.Linear(hidden_dim, 1)
            self.norm_penalized_params += [self.linear1.weight, self.linear2.weight]
        else:
            self.linear = nn.Linear(hidden_dim*2 + self.author_dims + self.subreddit_dims, 1)
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
    def forward(self, inputs, lengths, author_features=None, subreddit_features=None, **kwargs):
        if self.author_feature_shape is not None and author_features is None:
            raise ValueError("Need author features for forward")
        if self.subreddit_feature_shape is not None and subreddit_features is None:
            raise ValueError("Need subreddit features for forward")

        batch_size = inputs.shape[0]

        embedded_inputs = self.embeddings(inputs)

        if self.rnn_cell == 'GRU':
            rnn_states, _ = self.rnn(embedded_inputs, self.rnn_init_h.expand([-1,batch_size,-1]).contiguous())
        elif self.rnn_cell == 'LSTM':
            rnn_states, _ = self.rnn(embedded_inputs, (self.rnn_init_h.expand([-1,batch_size,-1]).contiguous,
                                     self.rnn_init_c.expand([-1,batch_size,-1]).contiguous()))


        # Select the final hidden state for each trajectory, taking its length into account
        # Using pack_padded_sequence would be even more efficient but would require
        # sorting all of the sequences - maybe later
        hidden_size = rnn_states.shape[2]

        idx = torch.ones((batch_size, 1, hidden_size), dtype=torch.long).to(self.device) * \
              (lengths - 1).view(-1, 1, 1)
        final_states = torch.gather(rnn_states, 1, idx).squeeze()

        dropped_out = self.dropout(final_states)

        if author_features is not None:
            if len(self.author_feature_shape) == 2:
                author_x = self.author_embeddings(author_features)
            else:
                author_x = author_features
            dropped_out = torch.cat((dropped_out, author_x), 1)

        if subreddit_features is not None:
            subreddit_x = self.subreddit_embeddings(subreddit_features)
            dropped_out = torch.cat((dropped_out, subreddit_x), 1)

        if self.second_linear_layer:
            x = self.linear1(dropped_out)
            x = self.relu(self.dropout(x))
            post_linear = self.linear2(x)
        else:
            post_linear = self.linear(dropped_out)

        probs = F.sigmoid(post_linear)  # sigmoid output for binary classification
        return probs

    def predict(self, inputs, lengths, author_features=None, subreddit_features=None):
        sigmoids = self(inputs, lengths, author_features, subreddit_features)
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
    def fit(self, train_data, val_datas):

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
        val_f1s = {val_set_name : [] for val_set_name in val_datas.keys()}
        primary_val_set_name = list(val_datas.keys())[0]

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
                outputs = self.model(batch['X'], batch['lengths'],
                                     batch['author_features'], batch['subreddit_features'])
                batch_train_f1s.append(f1_score(batch['Y'].detach(), torch.round(outputs.detach())))
                loss = criterion(outputs, batch['Y'].view(-1,1))
                if not self.balanced_setting:
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

            if self.verbose:
                print("\nAvg Loss: {}. Train (unpaired!) F1: {} ".format(
                    train_losses[-1], train_f1s[-1]), flush=True)

            for val_set_label, val_set in val_datas.items():
                val_predictions = self.predict(val_set['X'], val_set['lengths'],
                                               val_set['author_features'], val_set['subreddit_features'])
                rate_val_correct = accuracy_score(val_set['Y'], val_predictions)
                precision, recall, f1, support =  precision_recall_fscore_support(
                    val_set['Y'].detach(), val_predictions.detach())
                mean_f1 = np.mean(f1)
                val_f1s[val_set_label].append(mean_f1)
                if self.verbose:
                    print("On val set '{}' - Accuracy: {}. Precision: {}. Recall: {}. F1: {} (Mean {}).".format(
                        val_set_label, rate_val_correct, precision, recall, f1, mean_f1), flush=True)

            if self.early_stopping and epoch - np.argmax(val_f1s[primary_val_set_name]) >= self.epochs_to_persist:
                break
            if epoch - np.argmin(train_losses) >= self.epochs_to_persist:
                break

        print("\n\nTraining complete. Best (unpaired) train F1 {} from epoch {}".format(
            np.min(train_losses), np.argmin(train_losses)), flush=True)
        for val_set_label, val_set_f1s in val_f1s.items():
            print("Best F1 score {} from epoch {} on val set {}".format(
                np.max(val_set_f1s), np.argmax(val_set_f1s), val_set_label), flush=True)

        if self.output_graphs: self.make_graphs(train_losses, train_f1s, val_f1s)

        return train_losses, val_f1s

    # Note: this is not batch-ified; could make it so if it looks like it's being slow
    def predict(self, X, lengths, author_features=None, subreddit_features=None):
        self.model.eval()
        with torch.no_grad():
            predictions = None
            n = len(X)
            num_batches = n // self.batch_size + 1
            for b in range(num_batches):
                s, e = b*self.batch_size, (b+1)*self.batch_size
                X_batch, lengths_batch = X[s:e], lengths[s:e]
                authors_batch = author_features[s:e] if author_features is not None else None
                subreddits_batch = subreddit_features[s:e] if subreddit_features is not None else None

                if self.balanced_setting:
                    cur_predictions = self.predict_balanced(X_batch, lengths_batch, authors_batch, subreddits_batch)
                else:
                    cur_predictions = self.model.predict(X_batch, lengths_batch, authors_batch, subreddits_batch)

                if predictions is None: predictions = cur_predictions
                else: predictions = torch.cat((predictions, cur_predictions), 0)
        return predictions

    def predict_balanced(self, X, lengths, author_features=None, subreddit_features=None):
        probs = self.model(X, lengths, author_features, subreddit_features)
        assert len(probs) % 2 == 0
        n = len(probs) // 2
        predictions = torch.zeros(2*n)
        for i in range(n):
            if probs[2*i] > probs[2*i + 1]: predictions[2*i] = 1
            else: predictions[2*i + 1] = 1
        return predictions

    def make_graphs(self, train_losses, train_f1s, val_f1s):
        plt.plot(train_losses, label='Train loss')
        plt.plot(train_f1s, label='Train F1 (unpaired!)')
        for val_set_label, val_set_f1s in val_f1s.items():
            plt.plot(val_set_f1s, label='Holdout F1 for {}'.format(val_set_label))
        plt.legend()
        plt.xlabel('Epoch')
        plt.ylabel('Score')
        plt.title('Training curves')
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        plt.savefig(timestamp + '.png', bbox_inches='tight')

