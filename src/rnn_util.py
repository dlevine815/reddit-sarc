import random
from collections import OrderedDict, defaultdict

import torch
from sklearn.model_selection import train_test_split

from util import *
from rnn import NNClassifier, SarcasmRNN


def flatten(list_of_lists):
    return [x for l in list_of_lists for x in l]

# Run a super minimal experiment to make sure the net runs
def fast_nn_experiment():

    embed_lookup, word_to_idx = load_embeddings_by_index(GLOVE_FILES[50], 1000)
    glove_50_1000_fn = lambda: (embed_lookup, word_to_idx)

    results = nn_experiment(embed_fn=glove_50_1000_fn,
                          data_reader=pol_reader,
                          dataset_splitter=split_dataset_random_05,
                          lookup_phi=response_index_phi,
                          max_len=60,
                          author_phi_creator=None,
                          author_feature_shape_placeholder=None,
                          Module=SarcasmRNN,
                          rnn_cell='GRU',
                          hidden_dim=10,
                          dropout=0.1,
                          l2_lambda=1e-4,
                          lr=1e-3,
                          freeze_embeddings=True,
                          num_rnn_layers=2,
                          second_linear_layer=True,
                          batch_size=128,
                          max_epochs=10,
                          balanced_setting=True,
                          epochs_to_persist=3,
                          verbose=True,
                          progress_bar=True)

    return results


def author_comment_counts_phi_creator(train_set):
    num_sarcastic = defaultdict(int)
    num_non_sarcastic = defaultdict(int)

    for x in train_set:
        comment_labels = x['labels']
        comment_authors = x['response_authors']
        for a, l in zip(comment_authors, comment_labels):
            if l == 1: num_sarcastic[a] += 1
            else: num_non_sarcastic[a] += 1
    authors = set(num_sarcastic.keys()) | set(num_non_sarcastic.keys())
    return len(authors), lambda author: np.array([num_sarcastic[author], num_non_sarcastic[author]])


def author_addressee_index_phi_creator(train_set):
    return index_phi_creator(train_set, 'response_authors', True)

def author_index_phi_creator(train_set):
    return index_phi_creator(train_set, 'response_authors')

def subreddit_index_phi_creator(train_set):
    return index_phi_creator(train_set, 'response_subreddits')

def index_phi_creator(train_set, field_name, include_addressee=False):
    values = OrderedDict()
    i = 1
    for x in train_set:
        values_set = x[field_name]
        if include_addressee:
            values_set = values_set[:] + [x['ancestor_authors'][-1]]
        for a in values_set:
            if a not in values:
                values[a] = i
                i += 1
    return i, lambda x: values[x] if x in values else 0



split_dataset_random_01 = lambda x: split_dataset_random(x, .01)
split_dataset_random_05 = lambda x: split_dataset_random(x, .05)
def split_dataset_random(sets, val_proportion):
    train_set, val_set = train_test_split(sets, test_size=val_proportion)
    return train_set, OrderedDict({'{} holdout'.format(val_proportion) : val_set})

def split_dataset_random_plus_politics(sets):
    train_set, val_set = train_test_split(sets, test_size=.01)
    i = 0
    train_set2, pol_val_set = [], []
    for x in train_set:
        if x['response_subreddits'][0] == 'politics' and i < 200:
            pol_val_set.append(x)
            i += 1
        else:
            train_set2.append(x)
    return train_set2, OrderedDict({'{} holdout'.format(.01) : val_set,
                                   'politics holdout' : pol_val_set})


def build_and_split_dataset(data_reader, dataset_splitter, word_to_idx, lookup_phi, max_len, device,
                            author_phi_creator=None, author_feature_shape_placeholder=None,
                            embed_addresee=False,
                            subreddit_phi_creator=None, subreddit_embed_dim=None,
                            max_pts=None, **kwargs):

    sets = [x for x in data_reader()]
    if max_pts is not None:
        random.shuffle(sets)
        sets = sets[:max_pts]
    train_set, val_sets = dataset_splitter(sets)

    phi = lambda a,r: lookup_phi(a, r, word_to_idx, max_len=max_len)

    if author_phi_creator is not None:
        num_authors, author_phi = author_phi_creator(train_set)
        if len(author_feature_shape_placeholder) == 2:
            author_feature_shape = (num_authors, author_feature_shape_placeholder[1])
            author_feature_type = torch.long
        elif len(author_feature_shape_placeholder) == 1:
            author_feature_shape = author_feature_shape_placeholder
            author_feature_type = torch.float
        else:
            raise ValueError()
    else: num_authors, author_phi, author_feature_shape = None, None, None


    if subreddit_phi_creator is not None:
        num_subreddits, subreddit_phi = subreddit_phi_creator(train_set)
        subreddit_feature_shape = (num_subreddits, subreddit_embed_dim)
    else: num_subreddits, subreddit_phi, subreddit_feature_shape = None, None, None

    train_data = defaultdict(list)
    val_datas = {k : defaultdict(list) for k in val_sets.keys()}
    for unprocessed, processed in [(train_set, train_data), *[(val_sets[k], val_datas[k]) for k in val_sets.keys()]]:
        for x in unprocessed:
            processed['Y'].append(x['labels'])
            features_set, reversed_features_set, lengths = phi(x['ancestors'], x['responses'])
            processed['X'].append(features_set)
            processed['X_reversed'].append(reversed_features_set)
            processed['lengths'].append(lengths)
            if author_phi is not None:
                if embed_addresee:
                    addressee_ft = author_phi(x['ancestor_authors'][-1])
                    processed['author_features'].append(np.array([(addressee_ft, author_phi(a)) for a in x['response_authors']]))
                else:
                    processed['author_features'].append(np.array([author_phi(a) for a in x['response_authors']]))

            if subreddit_phi is not None:
                # All responses in a set should be from the same subreddit, but it's
                # just as easy to not depend on that assumption
                processed['subreddit_features'].append(np.array([subreddit_phi(sr) for sr in x['response_subreddits']]))

        processed['X'] = torch.tensor(np.concatenate(processed['X']), dtype=torch.long).to(device)
        processed['X_reversed'] = torch.tensor(np.concatenate(processed['X_reversed']), dtype=torch.long).to(device)
        processed['Y'] = torch.tensor(np.concatenate(processed['Y']), dtype=torch.float).to(device)
        processed['lengths'] = torch.tensor(np.concatenate(processed['lengths']), dtype=torch.long).to(device)

        if author_phi_creator is not None:
            processed['author_features'] = torch.tensor(np.concatenate(processed['author_features']),
                                                        dtype=author_feature_type).to(device)
        else: processed['author_features'] = None

        if subreddit_phi_creator is not None:
            processed['subreddit_features'] = torch.tensor(np.concatenate(
                processed['subreddit_features']),dtype=torch.long).to(device)
        else: processed['subreddit_features'] = None

    return train_data, val_datas, author_feature_shape, subreddit_feature_shape


def experiment_on_dataset(train_data, val_datas, author_feature_shape, subreddit_feature_shape,
                          embed_lookup, Module, rnn_cell, hidden_dim, dropout, l2_lambda, lr, device,
                          num_rnn_layers,
                          second_linear_layer,
                          batch_size,
                          ancestor_rnn=False,
                          attention_size=None,
                          balanced_setting=True,
                          recall_multiplier=None,
                          epochs_to_persist=3,
                          freeze_embeddings=True,
                          early_stopping=False,
                          max_epochs=100,
                          embed_addressee=False,
                          progress_bar=True,
                          verbose=True,
                          output_graphs=True):

    module_args = {'pretrained_weights' :  embed_lookup,
                   'hidden_dim'         :  hidden_dim,
                   'dropout'            :  dropout,
                   'freeze_embeddings'  :  freeze_embeddings,
                   'num_rnn_layers'     :  num_rnn_layers,
                   'ancestor_rnn'       :  ancestor_rnn,
                   'second_linear_layer':  second_linear_layer,
                   'attn_size'          :  attention_size,
                   'rnn_cell'           :  rnn_cell,
                   'embed_addressee'    :  embed_addressee}

    classifier = NNClassifier(batch_size=batch_size, max_epochs=max_epochs,
                              epochs_to_persist=epochs_to_persist, early_stopping=early_stopping,
                              verbose=verbose, progress_bar=progress_bar, output_graphs=output_graphs,
                              balanced_setting=balanced_setting, recall_multiplier=recall_multiplier,
                              l2_lambda=l2_lambda, lr=lr,
                              author_feature_shape=author_feature_shape,
                              subreddit_feature_shape=subreddit_feature_shape,
                              device=device,
                              Module=Module, module_args=module_args)

    best_val_f1, train_losses, train_f1s, val_f1s = classifier.fit(train_data, val_datas)
    return best_val_f1, train_losses, train_f1s, val_f1s


def nn_experiment(embed_fn, data_reader, dataset_splitter, lookup_phi, max_len,
                  Module, rnn_cell, hidden_dim, dropout, l2_lambda, lr,
                  num_rnn_layers,
                  second_linear_layer,
                  batch_size,
                  ancestor_rnn=False,
                  attention_size=None,
                  balanced_setting=True,
                  recall_multiplier=None,
                  epochs_to_persist=3,
                  freeze_embeddings=True,
                  early_stopping=False,
                  max_epochs=100,
                  max_pts=None,
                  author_phi_creator=None,
                  author_feature_shape_placeholder=None,
                  embed_addressee=False,
                  subreddit_phi_creator=None,
                  subreddit_embed_dim=None,
                  progress_bar=True,
                  verbose=True,
                  output_graphs=True,
                  ):

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print("Running on device: ", device, flush=True)

    embed_lookup, word_to_idx = embed_fn()
    embed_lookup = embed_lookup.to(device)

    train_data, val_datas, author_feature_shape, subreddit_feature_shape = \
        build_and_split_dataset(data_reader, dataset_splitter, word_to_idx, lookup_phi,
                max_len, device, author_phi_creator, author_feature_shape_placeholder, embed_addressee,
                subreddit_phi_creator, subreddit_embed_dim, max_pts)

    results = experiment_on_dataset(train_data, val_datas, author_feature_shape, subreddit_feature_shape,
                          embed_lookup, Module, rnn_cell, hidden_dim, dropout, l2_lambda, lr, device,
                          num_rnn_layers,
                          second_linear_layer,
                          batch_size,
                          ancestor_rnn,
                          attention_size,
                          balanced_setting,
                          recall_multiplier,
                          epochs_to_persist,
                          freeze_embeddings,
                          early_stopping,
                          max_epochs,
                          embed_addressee,
                          progress_bar,
                          verbose,
                          output_graphs)

    best_val_f1, train_losses, train_f1s, val_f1s = results
    return best_val_f1, train_losses, train_f1s, val_f1s


#Fixed params should be a dict of key:value pairs
#Params to try should be a dict from keys to lists of possible values
def crossval_nn_parameters(fixed_params, params_to_try, iterations, log_file):
    i = 0
    results = {}
    consecutive_duplicates = 0
    while True:
        cur_params = OrderedDict(fixed_params)
        for k, l in params_to_try.items():
            cur_params[k] = random.choice(l)
        cur_str = '\n'.join(["{}: {}".format(str(k), str(v)) for k,v in cur_params.items()])
        if cur_str in results:
            consecutive_duplicates += 1
        else:
            consecutive_duplicates = 0
            print("\n\n\nEvaluating parameters: \n", cur_str, flush=True)
            cur_results = nn_experiment(**cur_params)
            results[cur_str] =  cur_results
            #print("Parameters evaluated: \n{}\n\n".format(cur_results), flush=True)
            i += 1
        if i >= iterations or consecutive_duplicates >= 100 or i%10 == 0:
            best_results = sorted(results.items(), key=lambda x: x[0], reverse=True)
            print("Best results so far: ", flush=True)
            for k,v in best_results[:10]:
                print(k, flush=True)
                best_f1, train_losses, train_f1s, val_f1s = v
                print("\nBest (unpaired) train F1 {} and loss {} from epoch {}".format(
                    np.max(train_f1s), np.min(train_losses), np.argmin(train_losses)), flush=True)
                for val_set_label, val_set_f1s in val_f1s.items():
                    print("Best F1 score {} from epoch {} on val set {}".format(
                        np.max(val_set_f1s), np.argmax(val_set_f1s), val_set_label), flush=True)
                print('\n\n', flush=True)
        if i >= iterations or consecutive_duplicates >= 100:
            break

def reddit_tokenize(s):
    s = s.replace('*', ' * ')
    s = s.replace('\'', ' \'')
    return nltk.word_tokenize(s)

#This one ignores ancestors - generates seqs from responses only
def response_index_phi(ancestors, responses, word_to_ix, max_len):
    tokenizer = nltk.TweetTokenizer()
    n = len(responses)
    seqs = np.zeros([n, max_len], dtype=np.int_)
    seqs_reversed = np.zeros([n, max_len], dtype=np.int_)
    lengths = []

    for i, r in enumerate(responses):
        words = reddit_tokenize(r)
        seq_len = min(len(words), max_len)
        indices = [word_to_ix[w] if w in word_to_ix else 0 for w in words[:seq_len]]
        seqs[i, : seq_len] = indices
        seqs_reversed[i, : seq_len] = list(reversed(indices))
        #for w in words[:seq_len]:
        #    if w not in word_to_ix: print(w)
        lengths.append(seq_len)

    #return torch.from_numpy(seqs)
    return seqs, seqs_reversed, lengths


# Return a n x 2 x max_len structure
def response_and_ancestor_index_phi(ancestors, responses, word_to_ix, max_len):
    tokenizer = nltk.TweetTokenizer()
    n = len(responses)
    seqs = np.zeros([n, 2, max_len], dtype=np.int_)
    seqs_reversed = np.zeros([n, 2, max_len], dtype=np.int_)
    lengths = []

    ancestor_seq = np.zeros(max_len, dtype=np.int_)
    ancestor_seq_reversed = np.zeros(max_len, dtype=np.int_)
    ancestor_words = reddit_tokenize(ancestors[-1])
    ancestor_len = min(len(ancestor_words), max_len)
    ancestor_indices = [word_to_ix[w] if w in word_to_ix else 0 for w in ancestor_words[:ancestor_len]]
    ancestor_seq[: ancestor_len] = ancestor_indices
    ancestor_seq_reversed[: ancestor_len] = list(reversed(ancestor_indices))

    for i, r in enumerate(responses):
        words = reddit_tokenize(r)
        seq_len = min(len(words), max_len)
        indices = [word_to_ix[w] if w in word_to_ix else 0 for w in words[:seq_len]]
        seqs[i, 0] = ancestor_seq
        seqs[i, 1, :seq_len] = indices
        seqs_reversed[i, 0] = ancestor_seq_reversed
        seqs_reversed[i, 1, :seq_len] = list(reversed(indices))
        lengths.append([ancestor_len, seq_len])

    #return torch.from_numpy(seqs)
    return seqs, seqs_reversed, lengths


# num_to_read means don't bother reading past the first xx lines of the embeddings file
# Vocab means only read embeddings for the set of words in vocab
def load_embeddings_by_index(embeddings_file, num_to_read=None, vocab=None):
    if num_to_read is not None and num_to_read < 1:
        raise ValueError("Must read at least one embedding to get dimensionality!")

    lookup = [['UNK_placeholder']]
    word_to_idx = {}

    with open(embeddings_file) as f:
        if embeddings_file == FASTTEXT_FILE: next(f) # Skip first line
        for i, l in enumerate(f):
            if num_to_read is not None and i >= num_to_read: break
            idx = i+1 # account for UNK token at beginning
            fields = l.strip().split()
            word = fields[0]
            if vocab and word not in vocab: continue
            vec = np.array(fields[1:], dtype=np.float32)
            lookup.append(vec)
            assert len(lookup) == idx + 1
            word_to_idx[word] = idx

        # Fill in the UNK token now that we know the embedding length
        lookup[0] = np.zeros(len(lookup[1]))
    lookup = np.asarray(lookup, np.float32)
    torch_lookup = torch.from_numpy(lookup)

    return torch_lookup, word_to_idx
