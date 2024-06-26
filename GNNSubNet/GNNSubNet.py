# GNNSubNet.py
# Authors: Bastian Pfeifer <https://github.com/pievos101>, Marcus D. Bloice <https://github.com/mdbloice>
from urllib.parse import _NetlocResultMixinStr
import numpy as np
import random
#from scipy.sparse.extract import find
from scipy.sparse import find
import torch
import torch.nn as nn
import torch.nn.functional as F
from sklearn.metrics import confusion_matrix
from sklearn.model_selection import train_test_split
from torch.nn.modules import conv
from torch_geometric import data
from torch_geometric.data import DataLoader, Batch
from pathlib import Path
import copy
from tqdm import tqdm
import os
import requests
import pandas as pd
import io
#from collections.abc import Mapping

from torch_geometric.data.data import Data
from torch_geometric.loader import DataLoader

from .gnn_training_utils import check_if_graph_is_connected, pass_data_iteratively
from .dataset import generate, load_OMICS_dataset, convert_to_s2vgraph
from .gnn_explainer import GNNExplainer
from .graphcnn  import GraphCNN
from .graphcheb import GraphCheb, ChebConvNet, test_model_acc, test_model

from .community_detection import find_communities
from .edge_importance import calc_edge_importance

from torch_geometric.nn.conv.cheb_conv import ChebConv




################################ Add graph attention layer #####################################

from tensorflow import keras
from tensorflow.keras import layers

class GraphAttention(layers.Layer):
    def __init__(
        self,
        units,
        kernel_initializer="glorot_uniform",
        kernel_regularizer=None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.units = units
        self.kernel_initializer = keras.initializers.get(kernel_initializer)
        self.kernel_regularizer = keras.regularizers.get(kernel_regularizer)

    def build(self, input_shape):

        self.kernel = self.add_weight(
            shape=(input_shape[0][-1], self.units),
            trainable=True,
            initializer=self.kernel_initializer,
            regularizer=self.kernel_regularizer,
            name="kernel",
        )
        self.kernel_attention = self.add_weight(
            shape=(self.units * 2, 1),
            trainable=True,
            initializer=self.kernel_initializer,
            regularizer=self.kernel_regularizer,
            name="kernel_attention",
        )
        self.built = True

    def call(self, inputs):
        node_states, edges = inputs

        # Linearly transform node states
        node_states_transformed = tf.matmul(node_states, self.kernel)

        # (1) Compute pair-wise attention scores
        node_states_expanded = tf.gather(node_states_transformed, edges)
        node_states_expanded = tf.reshape(
            node_states_expanded, (tf.shape(edges)[0], -1)
        )
        attention_scores = tf.nn.leaky_relu(
            tf.matmul(node_states_expanded, self.kernel_attention)
        )
        attention_scores = tf.squeeze(attention_scores, -1)

        # (2) Normalize attention scores
        attention_scores = tf.math.exp(tf.clip_by_value(attention_scores, -2, 2))
        attention_scores_sum = tf.math.unsorted_segment_sum(
            data=attention_scores,
            segment_ids=edges[:, 0],
            num_segments=tf.reduce_max(edges[:, 0]) + 1,
        )
        attention_scores_sum = tf.repeat(
            attention_scores_sum, tf.math.bincount(tf.cast(edges[:, 0], "int32"))
        )
        attention_scores_norm = attention_scores / attention_scores_sum

        # (3) Gather node states of neighbors, apply attention scores and aggregate
        node_states_neighbors = tf.gather(node_states_transformed, edges[:, 1])
        out = tf.math.unsorted_segment_sum(
            data=node_states_neighbors * attention_scores_norm[:, tf.newaxis],
            segment_ids=edges[:, 0],
            num_segments=tf.shape(node_states)[0],
        )
        return out
    
##################################################################################################

from torch.utils.data import DataLoader, Dataset

class GATLayer(torch.nn.Module):
    """
    Implementation #3 was inspired by PyTorch Geometric: https://github.com/rusty1s/pytorch_geometric

    But, it's hopefully much more readable! (and of similar performance)

    """
    
    # We'll use these constants in many functions so just extracting them here as member fields
    src_nodes_dim = 0  # position of source nodes in edge index
    trg_nodes_dim = 1  # position of target nodes in edge index

    # These may change in the inductive setting - leaving it like this for now (not future proof)
    nodes_dim = 0      # node dimension (axis is maybe a more familiar term nodes_dim is the position of "N" in tensor)
    head_dim = 1       # attention head dim

    def __init__(self, num_in_features, num_out_features, num_of_heads, concat=True, activation=nn.ELU(),
                 dropout_prob=0.6, add_skip_connection=True, bias=True, log_attention_weights=False):

        super().__init__()

        self.num_of_heads = num_of_heads
        self.num_out_features = num_out_features
        self.concat = concat  # whether we should concatenate or average the attention heads
        self.add_skip_connection = add_skip_connection

        #
        # Trainable weights: linear projection matrix (denoted as "W" in the paper), attention target/source
        # (denoted as "a" in the paper) and bias (not mentioned in the paper but present in the official GAT repo)
        #

        # You can treat this one matrix as num_of_heads independent W matrices
        self.linear_proj = nn.Linear(num_in_features, num_of_heads * num_out_features, bias=False)

        # After we concatenate target node (node i) and source node (node j) we apply the "additive" scoring function
        # which gives us un-normalized score "e". Here we split the "a" vector - but the semantics remain the same.
        # Basically instead of doing [x, y] (concatenation, x/y are node feature vectors) and dot product with "a"
        # we instead do a dot product between x and "a_left" and y and "a_right" and we sum them up
        self.scoring_fn_target = nn.Parameter(torch.Tensor(1, num_of_heads, num_out_features))
        self.scoring_fn_source = nn.Parameter(torch.Tensor(1, num_of_heads, num_out_features))

        # Bias is definitely not crucial to GAT - feel free to experiment (I pinged the main author, Petar, on this one)
        if bias and concat:
            self.bias = nn.Parameter(torch.Tensor(num_of_heads * num_out_features))
        elif bias and not concat:
            self.bias = nn.Parameter(torch.Tensor(num_out_features))
        else:
            self.register_parameter('bias', None)

        if add_skip_connection:
            self.skip_proj = nn.Linear(num_in_features, num_of_heads * num_out_features, bias=False)
        else:
            self.register_parameter('skip_proj', None)

        #
        # End of trainable weights
        #

        self.leakyReLU = nn.LeakyReLU(0.2)  # using 0.2 as in the paper, no need to expose every setting
        self.activation = activation
        # Probably not the nicest design but I use the same module in 3 locations, before/after features projection
        # and for attention coefficients. Functionality-wise it's the same as using independent modules.
        self.dropout = nn.Dropout(p=dropout_prob)

        self.log_attention_weights = log_attention_weights  # whether we should log the attention weights
        self.attention_weights = None  # for later visualization purposes, I cache the weights here

        self.init_params()
        
    def forward(self, data):
        #
        # Step 1: Linear Projection + regularization
        #

        in_nodes_features, edge_index = data  # unpack data
        num_of_nodes = in_nodes_features.shape[self.nodes_dim]
        assert edge_index.shape[0] == 2, f'Expected edge index with shape=(2,E) got {edge_index.shape}'

        # shape = (N, FIN) where N - number of nodes in the graph, FIN - number of input features per node
        # We apply the dropout to all of the input node features (as mentioned in the paper)
        in_nodes_features = self.dropout(in_nodes_features)

        # shape = (N, FIN) * (FIN, NH*FOUT) -> (N, NH, FOUT) where NH - number of heads, FOUT - num of output features
        # We project the input node features into NH independent output features (one for each attention head)
        nodes_features_proj = self.linear_proj(in_nodes_features).view(-1, self.num_of_heads, self.num_out_features)

        nodes_features_proj = self.dropout(nodes_features_proj)  # in the official GAT imp they did dropout here as well

        #
        # Step 2: Edge attention calculation
        #

        # Apply the scoring function (* represents element-wise (a.k.a. Hadamard) product)
        # shape = (N, NH, FOUT) * (1, NH, FOUT) -> (N, NH, 1) -> (N, NH) because sum squeezes the last dimension
        # Optimization note: torch.sum() is as performant as .sum() in my experiments
        scores_source = (nodes_features_proj * self.scoring_fn_source).sum(dim=-1)
        scores_target = (nodes_features_proj * self.scoring_fn_target).sum(dim=-1)

        # We simply copy (lift) the scores for source/target nodes based on the edge index. Instead of preparing all
        # the possible combinations of scores we just prepare those that will actually be used and those are defined
        # by the edge index.
        # scores shape = (E, NH), nodes_features_proj_lifted shape = (E, NH, FOUT), E - number of edges in the graph
        scores_source_lifted, scores_target_lifted, nodes_features_proj_lifted = self.lift(scores_source, scores_target, nodes_features_proj, edge_index)
        scores_per_edge = self.leakyReLU(scores_source_lifted + scores_target_lifted)

        # shape = (E, NH, 1)
        attentions_per_edge = self.neighborhood_aware_softmax(scores_per_edge, edge_index[self.trg_nodes_dim], num_of_nodes)
        # Add stochasticity to neighborhood aggregation
        attentions_per_edge = self.dropout(attentions_per_edge)

        #
        # Step 3: Neighborhood aggregation
        #

        # Element-wise (aka Hadamard) product. Operator * does the same thing as torch.mul
        # shape = (E, NH, FOUT) * (E, NH, 1) -> (E, NH, FOUT), 1 gets broadcast into FOUT
        nodes_features_proj_lifted_weighted = nodes_features_proj_lifted * attentions_per_edge

        # This part sums up weighted and projected neighborhood feature vectors for every target node
        # shape = (N, NH, FOUT)
        out_nodes_features = self.aggregate_neighbors(nodes_features_proj_lifted_weighted, edge_index, in_nodes_features, num_of_nodes)

        #
        # Step 4: Residual/skip connections, concat and bias
        #

        out_nodes_features = self.skip_concat_bias(attentions_per_edge, in_nodes_features, out_nodes_features)
        return (out_nodes_features, edge_index)

    #
    # Helper functions (without comments there is very little code so don't be scared!)
    #

    def neighborhood_aware_softmax(self, scores_per_edge, trg_index, num_of_nodes):
        """
        As the fn name suggest it does softmax over the neighborhoods. Example: say we have 5 nodes in a graph.
        Two of them 1, 2 are connected to node 3. If we want to calculate the representation for node 3 we should take
        into account feature vectors of 1, 2 and 3 itself. Since we have scores for edges 1-3, 2-3 and 3-3
        in scores_per_edge variable, this function will calculate attention scores like this: 1-3/(1-3+2-3+3-3)
        (where 1-3 is overloaded notation it represents the edge 1-3 and its (exp) score) and similarly for 2-3 and 3-3
         i.e. for this neighborhood we don't care about other edge scores that include nodes 4 and 5.

        Note:
        Subtracting the max value from logits doesn't change the end result but it improves the numerical stability
        and it's a fairly common "trick" used in pretty much every deep learning framework.
        Check out this link for more details:

        https://stats.stackexchange.com/questions/338285/how-does-the-subtraction-of-the-logit-maximum-improve-learning

        """
        # Calculate the numerator. Make logits <= 0 so that e^logit <= 1 (this will improve the numerical stability)
        scores_per_edge = scores_per_edge - scores_per_edge.max()
        exp_scores_per_edge = scores_per_edge.exp()  # softmax

        # Calculate the denominator. shape = (E, NH)
        neigborhood_aware_denominator = self.sum_edge_scores_neighborhood_aware(exp_scores_per_edge, trg_index, num_of_nodes)

        # 1e-16 is theoretically not needed but is only there for numerical stability (avoid div by 0) - due to the
        # possibility of the computer rounding a very small number all the way to 0.
        attentions_per_edge = exp_scores_per_edge / (neigborhood_aware_denominator + 1e-16)

        # shape = (E, NH) -> (E, NH, 1) so that we can do element-wise multiplication with projected node features
        return attentions_per_edge.unsqueeze(-1)

    def sum_edge_scores_neighborhood_aware(self, exp_scores_per_edge, trg_index, num_of_nodes):
        # The shape must be the same as in exp_scores_per_edge (required by scatter_add_) i.e. from E -> (E, NH)
        trg_index_broadcasted = self.explicit_broadcast(trg_index, exp_scores_per_edge)

        # shape = (N, NH), where N is the number of nodes and NH the number of attention heads
        size = list(exp_scores_per_edge.shape)  # convert to list otherwise assignment is not possible
        size[self.nodes_dim] = num_of_nodes
        neighborhood_sums = torch.zeros(size, dtype=exp_scores_per_edge.dtype, device=exp_scores_per_edge.device)

        # position i will contain a sum of exp scores of all the nodes that point to the node i (as dictated by the
        # target index)
        neighborhood_sums.scatter_add_(self.nodes_dim, trg_index_broadcasted, exp_scores_per_edge)

        # Expand again so that we can use it as a softmax denominator. e.g. node i's sum will be copied to
        # all the locations where the source nodes pointed to i (as dictated by the target index)
        # shape = (N, NH) -> (E, NH)
        return neighborhood_sums.index_select(self.nodes_dim, trg_index)

    def aggregate_neighbors(self, nodes_features_proj_lifted_weighted, edge_index, in_nodes_features, num_of_nodes):
        size = list(nodes_features_proj_lifted_weighted.shape)  # convert to list otherwise assignment is not possible
        size[self.nodes_dim] = num_of_nodes  # shape = (N, NH, FOUT)
        out_nodes_features = torch.zeros(size, dtype=in_nodes_features.dtype, device=in_nodes_features.device)

        # shape = (E) -> (E, NH, FOUT)
        trg_index_broadcasted = self.explicit_broadcast(edge_index[self.trg_nodes_dim], nodes_features_proj_lifted_weighted)
        # aggregation step - we accumulate projected, weighted node features for all the attention heads
        # shape = (E, NH, FOUT) -> (N, NH, FOUT)
        out_nodes_features.scatter_add_(self.nodes_dim, trg_index_broadcasted, nodes_features_proj_lifted_weighted)

        return out_nodes_features

    def lift(self, scores_source, scores_target, nodes_features_matrix_proj, edge_index):
        """
        Lifts i.e. duplicates certain vectors depending on the edge index.
        One of the tensor dims goes from N -> E (that's where the "lift" comes from).

        """
        src_nodes_index = edge_index[self.src_nodes_dim]
        trg_nodes_index = edge_index[self.trg_nodes_dim]

        # Using index_select is faster than "normal" indexing (scores_source[src_nodes_index]) in PyTorch!
        scores_source = scores_source.index_select(self.nodes_dim, src_nodes_index)
        scores_target = scores_target.index_select(self.nodes_dim, trg_nodes_index)
        nodes_features_matrix_proj_lifted = nodes_features_matrix_proj.index_select(self.nodes_dim, src_nodes_index)

        return scores_source, scores_target, nodes_features_matrix_proj_lifted

    def explicit_broadcast(self, this, other):
        # Append singleton dimensions until this.dim() == other.dim()
        for _ in range(this.dim(), other.dim()):
            this = this.unsqueeze(-1)

        # Explicitly expand so that shapes are the same
        return this.expand_as(other)

    def init_params(self):
        """
        The reason we're using Glorot (aka Xavier uniform) initialization is because it's a default TF initialization:
            https://stackoverflow.com/questions/37350131/what-is-the-default-variable-initializer-in-tensorflow

        The original repo was developed in TensorFlow (TF) and they used the default initialization.
        Feel free to experiment - there may be better initializations depending on your problem.

        """
        nn.init.xavier_uniform_(self.linear_proj.weight)
        nn.init.xavier_uniform_(self.scoring_fn_target)
        nn.init.xavier_uniform_(self.scoring_fn_source)

        if self.bias is not None:
            torch.nn.init.zeros_(self.bias)

    def skip_concat_bias(self, attention_coefficients, in_nodes_features, out_nodes_features):
        if self.log_attention_weights:  # potentially log for later visualization in playground.py
            self.attention_weights = attention_coefficients

        if self.add_skip_connection:  # add skip or residual connection
            if out_nodes_features.shape[-1] == in_nodes_features.shape[-1]:  # if FIN == FOUT
                # unsqueeze does this: (N, FIN) -> (N, 1, FIN), out features are (N, NH, FOUT) so 1 gets broadcast to NH
                # thus we're basically copying input vectors NH times and adding to processed vectors
                out_nodes_features += in_nodes_features.unsqueeze(1)
            else:
                # FIN != FOUT so we need to project input feature vectors into dimension that can be added to output
                # feature vectors. skip_proj adds lots of additional capacity which may cause overfitting.
                out_nodes_features += self.skip_proj(in_nodes_features).view(-1, self.num_of_heads, self.num_out_features)

        if self.concat:
            # shape = (N, NH, FOUT) -> (N, NH*FOUT)
            out_nodes_features = out_nodes_features.view(-1, self.num_of_heads * self.num_out_features)
        else:
            # shape = (N, NH, FOUT) -> (N, FOUT)
            out_nodes_features = out_nodes_features.mean(dim=self.head_dim)

        if self.bias is not None:
            out_nodes_features += self.bias

        return out_nodes_features if self.activation is None else self.activation(out_nodes_features)



################################ Add graph attention layer #####################################




class GNNSubNet(object):
    """
    The class GNNSubSet represents the main user API for the
    GNN-SubNet package.
    """
    def __init__(self, location=None, ppi=None, features=None, target=None, cutoff=950, normalize=True, random_seed=None) -> None:

        self.location = location
        self.ppi = ppi
        self.features = features
        self.target = target
        self.dataset = None
        self.model_status = None
        self.model = None
        self.gene_names = None
        self.accuracy = None
        self.confusion_matrix = None
        self.test_loss = None
        
        
        self.use_attention = False  
        self.attention_units = 64   # The number of output units of the GAT layer
        
        
######### Set the random seed if provided ###########
        if random_seed is not None:
            random.seed(random_seed)
            np.random.seed(random_seed)
#####################################################

        # Flags for internal use (hidden from user)
        self._explainer_run = False

        if ppi == None:
            return None

        dataset, gene_names = load_OMICS_dataset(self.ppi, self.features, self.target, True, cutoff, normalize)

         # Check whether graph is connected
        check = check_if_graph_is_connected(dataset[0].edge_index)
        print("Graph is connected ", check)

        if check == False:

            print("Calculate subgraph ...")
            dataset, gene_names = load_OMICS_dataset(self.ppi, self.features, self.target, False, cutoff, normalize)

        check = check_if_graph_is_connected(dataset[0].edge_index)
        print("Graph is connected ", check)

        #print('\n')
        print('##################')
        print("# DATASET LOADED #")
        print('##################')
        #print('\n')

        self.dataset = dataset
        self.true_class = None
        self.gene_names = gene_names
        self.s2v_test_dataset = None
        self.edges =  np.transpose(np.array(dataset[0].edge_index))

        self.edge_mask = None
        self.node_mask = None
        self.node_mask_matrix = None
        self.modules = None
        self.module_importances = None

    def summary(self):
        """
        Print a summary for the GNNSubSet object's current state.
        """
        print("")
        print("Number of nodes:", len(self.dataset[0].x))
        print("Number of edges:", self.edges.shape[0])
        print("Number of modalities:",self.dataset[0].x.shape[1])
        
        
##########################################################################
    
    #def add_graph_attention_layer(self, units, **kwargs):
        #self.graph_attention = GraphAttention(units, **kwargs)
        
##########################################################################


    def train(self, epoch_nr = 20, method="graphcnn", learning_rate=0.01, use_attention=False):

        
        #if use_attention:
            #self.add_graph_attention_layer(units=64)
            
##################################################################################### 

        self.use_attention = use_attention
        if self.use_attention:
            self.add_graph_attention_layer(self.attention_units)  # adding a GAT layer
            
#####################################################################################
        
        if method=="chebconv":
            print("chebconv for training ...")
            self.train_chebconv(epoch_nr = epoch_nr)
            self.classifier="chebconv"

        if method=="graphcnn":
            print("graphcnn for training ...")
            self.train_graphcnn(epoch_nr = epoch_nr, learning_rate=learning_rate)
            self.classifier="graphcnn"

        if method=="graphcheb":
            print("graphcheb for training ...")
            self.train_graphcheb(epoch_nr = epoch_nr)
            self.classifier="graphcheb"

        if method=="chebnet":
            print("chebnet for training ...")
            self.train_chebnet(epoch_nr = epoch_nr)
            self.classifier="chebnet"
            
##################################################################################### 

      #  self.use_attention = use_attention
      #  if self.use_attention:
      #      self.add_graph_attention_layer(self.attention_units)  # adding a GAT layer
            
#####################################################################################
            
    
            
    def add_graph_attention_layer(self, units):
        if self.use_attention:
            # Add GAT layer to existing model
            gat_layer = GATLayer(
                num_in_features=self.dataset[0].x.shape[1],  # input feature number
                num_out_features=units,
                num_of_heads=8  
            )
            # Add GAT layer and update model
            if self.model is not None:
                print("Adding GAT to:", self.model)  
                self.model = nn.Sequential(gat_layer, self.model)
            else:
                print("No existing model. Creating a new Sequential.")
                self.model = nn.Sequential(gat_layer)
                
#####################################################################################      


    def explain(self, n_runs=1, classifier="graphcnn", communities=True):

        if self.classifier=="chebconv":
            self.explain_chebconv(n_runs=n_runs, communities=communities)

        if self.classifier=="graphcnn":
            self.explain_graphcnn(n_runs=n_runs, communities=communities)      
    
        if self.classifier=="graphcheb":
            self.explain_graphcheb(n_runs=n_runs, communities=communities)

        if self.classifier=="chebnet":
            self.explain_graphcheb(n_runs=n_runs, communities=communities)



    def predict(self, gnnsubnet_test, classifier="graphcnn"):
    
        if self.classifier=="chebconv":
            pred = self.predict_chebconv(gnnsubnet_test=gnnsubnet_test)

        if self.classifier=="graphcnn":
            pred = self.predict_graphcnn(gnnsubnet_test=gnnsubnet_test)      
        
        if self.classifier=="graphcheb":
            pred = self.predict_graphcheb(gnnsubnet_test=gnnsubnet_test)
               
        if self.classifier=="chebnet":
            pred = self.predict_graphcheb(gnnsubnet_test=gnnsubnet_test)
               
        pred = np.array(pred)
        pred = pred.reshape(1, pred.size)

        return pred

    def train_chebnet(self, epoch_nr=25, shuffle=True, weights=False,
                        hidden_channels=10,
                        K=10,
                        layers_nr=1,
                        num_classes=2):
        """
        ---
        """
        use_weights = False

        dataset = self.dataset
        gene_names = self.gene_names

        graphs_class_0_list = []
        graphs_class_1_list = []
        for graph in dataset:
            if graph.y.detach().cpu().numpy() == 0:
                graphs_class_0_list.append(graph)
            else:
                graphs_class_1_list.append(graph)

        graphs_class_0_len = len(graphs_class_0_list)
        graphs_class_1_len = len(graphs_class_1_list)

        print(f"Graphs class 0: {graphs_class_0_len}, Graphs class 1: {graphs_class_1_len}")

        ########################################################################################################################
        # Downsampling of the class that contains more elements ===========================================================
        # ########################################################################################################################

        if graphs_class_0_len >= graphs_class_1_len:
            random_graphs_class_0_list = random.sample(graphs_class_0_list, graphs_class_1_len)
            balanced_dataset_list = graphs_class_1_list + random_graphs_class_0_list

        if graphs_class_0_len < graphs_class_1_len:
            random_graphs_class_1_list = random.sample(graphs_class_1_list, graphs_class_0_len)
            balanced_dataset_list = graphs_class_0_list + random_graphs_class_1_list

        # print(len(random_graphs_class_0_list))
        # print(len(random_graphs_class_1_list))

        random.shuffle(balanced_dataset_list)
        print(f"Length of balanced dataset list: {len(balanced_dataset_list)}")

        list_len = len(balanced_dataset_list)
        # print(list_len)
        train_set_len = int(list_len * 4 / 5)
        train_dataset_list = balanced_dataset_list[:train_set_len]
        test_dataset_list = balanced_dataset_list[train_set_len:]

        train_graph_class_0_nr = 0
        train_graph_class_1_nr = 0
        for graph in train_dataset_list:
            if graph.y.detach().cpu().numpy() == 0:
                train_graph_class_0_nr += 1
            else:
                train_graph_class_1_nr += 1
        print(f"Train graph class 0: {train_graph_class_0_nr}, train graph class 1: {train_graph_class_1_nr}")

        test_graph_class_0_nr = 0
        test_graph_class_1_nr = 0
        for graph in test_dataset_list:
            if graph.y.detach().cpu().numpy() == 0:
                test_graph_class_0_nr += 1
            else:
                test_graph_class_1_nr += 1
        print(f"Validation graph class 0: {test_graph_class_0_nr}, validation graph class 1: {test_graph_class_1_nr}")

        # s2v_train_dataset = convert_to_s2vgraph(train_dataset_list)
        # s2v_test_dataset  = convert_to_s2vgraph(test_dataset_list)
        s2v_train_dataset = train_dataset_list
        s2v_test_dataset = test_dataset_list

        model_path = 'omics_model.pth'
        no_of_features = dataset[0].x.shape[1]
        nodes_per_graph_nr = dataset[0].x.shape[0]
        print("\tnodes_per_graph_nr", nodes_per_graph_nr)

        input_dim = no_of_features
        n_classes = 2

        #device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        model = ChebConvNet(input_channels=1, n_features=nodes_per_graph_nr, n_channels=2, n_classes=2, K=8, n_layers=1)
        #print(model)
        #model.to(device)
        optimizer = torch.optim.Adam(model.parameters(), lr=0.001, weight_decay=1e-6)
        # lr_scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer=optimizer, gamma=0.9,
        #                                                       last_epoch=-1)
        criterion = torch.nn.CrossEntropyLoss()

        model.train()
        min_loss = 50

        best_model = ChebConvNet(input_channels=1, n_features=nodes_per_graph_nr, n_channels=2, n_classes=2, K=8, n_layers=1)
        #best_model.to(device)
        # best_model = ChebConv(in_channels=1, out_channels=2, K=10)

        min_val_loss = 1000000.0
        n_epochs_stop = 25
        epochs_no_improve = 0
        batch_size = 100

        train_loader = DataLoader(s2v_train_dataset, batch_size=batch_size, shuffle=True)
        test_loader = DataLoader(s2v_test_dataset, batch_size=batch_size, shuffle=False)

        for epoch in range(epoch_nr):
            running_loss = 0.0
            steps = 0
            model.train()
            # data_pbar_loader = tqdm(train_loader, unit='batch')
            for data in train_loader:
                out = model(x=data.x, edge_index=data.edge_index, batch=data.batch)
                loss = criterion(out, data.y)  # Compute the loss.
                loss.backward()  # Derive gradients.
                optimizer.step()  # Update parameters based on gradients.
                optimizer.zero_grad()  # Clear gradients.
                running_loss += loss.item()
                steps += 1

            epoch_loss = running_loss / steps
            model.eval()
            acc_train = test_model_acc(train_loader, model)
            val_loss, val_acc, _ , _ = test_model(test_loader, model, criterion)

            print()
            print(f'Epoch: {epoch:03d}, Train Loss: {epoch_loss:.4f}, Train Acc: {acc_train:.4f}, Val Loss: {val_loss:.4f}, Val Acc: {val_acc:.4f}', end='\t')
            # print('Epoch {}, loss {:.4f}'.format(epoch, epoch_loss))
            # print(f"Train Acc {acc_train:.4f}")

            # data_pbar_loader.set_description('epoch: %d' % (epoch))
            # val_loss = 0
            #
            # tr = DataLoader(s2v_test_dataset, batch_size=len(s2v_test_dataset), shuffle=False)
            # for vv in tr:
            #     # print("\toutput test")
            #     output = model(vv.x, vv.edge_index, vv.batch)
            #     # print("\toutput", output)
            #
            # # output = pass_data_iteratively(model, s2v_test_dataset)
            #
            # pred = output.max(1, keepdim=True)[1]
            # labels = torch.LongTensor([graph.y for graph in s2v_test_dataset])
            # if use_weights:
            #     loss = nn.CrossEntropyLoss(weight=weight)(output, labels)
            # else:
            #     loss = nn.CrossEntropyLoss()(output, labels)
            # val_loss += loss

            # print('Epoch {}, val_loss {:.4f}'.format(epoch, val_loss))
            if val_loss < min_val_loss and epoch > 2: # to go through at least 2 epochs
                print(f"Saving best model with validation loss {val_loss:.4f}", end="")
                best_model = copy.deepcopy(model)
                epochs_no_improve = 0
                min_val_loss = val_loss
            else:
                epochs_no_improve += 1
                # Check early stopping condition
                if epochs_no_improve == n_epochs_stop:
                    print('Early stopping!')
                    # model.load_state_dict(best_model.state_dict())
                    break
        #
        # confusion_array = []
        # true_class_array = []
        # predicted_class_array = []
        # model.eval()
        # correct = 0
        # true_class_array = []
        # predicted_class_array = []

        # loading the parameters of the best model
        model.load_state_dict(best_model.state_dict())

        _, _, true_labels, predicted_labels = test_model(test_loader, model, criterion)


        # test_loss = 0
        #
        # model.load_state_dict(best_model.state_dict())
        #
        # tr = DataLoader(s2v_test_dataset, batch_size=len(s2v_test_dataset), shuffle=False)
        # for vv in tr:
        #     output = model(vv.x, vv.edge_index, vv.batch)
        #
        # # output = pass_data_iteratively(model, s2v_test_dataset)
        # output = np.array(output.detach())
        # predicted_class = output.argmax(1, keepdims=True)
        #
        # predicted_class = list(predicted_class)
        #
        # labels = torch.LongTensor([graph.y for graph in s2v_test_dataset])
        # correct = torch.tensor(np.array(predicted_class)).eq(
        #     labels.view_as(torch.tensor(np.array(predicted_class)))).sum().item()



        confusion_matrix_gnn = confusion_matrix(true_labels, predicted_labels)
        print("\nConfusion matrix (Validation set):\n")
        print(confusion_matrix_gnn)

        from sklearn.metrics import balanced_accuracy_score
        acc_bal = balanced_accuracy_score(true_labels, predicted_labels)

        print("Validation balanced accuracy: {}".format(acc_bal))

        model.train()

        self.model_status = 'Trained'
        self.model = copy.deepcopy(model)
        self.accuracy = acc_bal
        self.confusion_matrix = confusion_matrix_gnn
        # self.test_loss = test_loss
        self.s2v_test_dataset = s2v_test_dataset
        self.predictions = predicted_labels
        self.true_class = true_labels
    
    
    def train_graphcheb(self, epoch_nr = 20, shuffle=True, weights=False,
                    hidden_channels=7,
                    K=5,
                    layers_nr=2,
                    num_classes=2):
        """
        ---
        """
        use_weights = False

        dataset = self.dataset
        gene_names = self.gene_names

        graphs_class_0_list = []
        graphs_class_1_list = []
        for graph in dataset:
            if graph.y.numpy() == 0:
                graphs_class_0_list.append(graph)
            else:
                graphs_class_1_list.append(graph)

        graphs_class_0_len = len(graphs_class_0_list)
        graphs_class_1_len = len(graphs_class_1_list)

        print(f"Graphs class 0: {graphs_class_0_len}, Graphs class 1: {graphs_class_1_len}")

        ########################################################################################################################
        # Downsampling of the class that contains more elements ===========================================================
        # ########################################################################################################################

        if graphs_class_0_len >= graphs_class_1_len:
            random_graphs_class_0_list = random.sample(graphs_class_0_list, graphs_class_1_len)
            balanced_dataset_list = graphs_class_1_list + random_graphs_class_0_list

        if graphs_class_0_len < graphs_class_1_len:
            random_graphs_class_1_list = random.sample(graphs_class_1_list, graphs_class_0_len)
            balanced_dataset_list = graphs_class_0_list + random_graphs_class_1_list

        #print(len(random_graphs_class_0_list))
        #print(len(random_graphs_class_1_list))

        random.shuffle(balanced_dataset_list)
        print(f"Length of balanced dataset list: {len(balanced_dataset_list)}")

        list_len = len(balanced_dataset_list)
        #print(list_len)
        train_set_len = int(list_len * 4 / 5)
        train_dataset_list = balanced_dataset_list[:train_set_len]
        test_dataset_list  = balanced_dataset_list[train_set_len:]

        train_graph_class_0_nr = 0
        train_graph_class_1_nr = 0
        for graph in train_dataset_list:
            if graph.y.numpy() == 0:
                train_graph_class_0_nr += 1
            else:
                train_graph_class_1_nr += 1
        print(f"Train graph class 0: {train_graph_class_0_nr}, train graph class 1: {train_graph_class_1_nr}")

        test_graph_class_0_nr = 0
        test_graph_class_1_nr = 0
        for graph in test_dataset_list:
            if graph.y.numpy() == 0:
                test_graph_class_0_nr += 1
            else:
                test_graph_class_1_nr += 1
        print(f"Validation graph class 0: {test_graph_class_0_nr}, validation graph class 1: {test_graph_class_1_nr}")

        #s2v_train_dataset = convert_to_s2vgraph(train_dataset_list)
        #s2v_test_dataset  = convert_to_s2vgraph(test_dataset_list)
        s2v_train_dataset = train_dataset_list
        s2v_test_dataset  = test_dataset_list

        model_path = 'omics_model.pth'
        no_of_features = dataset[0].x.shape[1]
        nodes_per_graph_nr = dataset[0].x.shape[0]

        input_dim = no_of_features
        n_classes = 2

        model = GraphCheb(
                    num_node_features=input_dim,
                    hidden_channels=hidden_channels,
                    K=K,
                    layers_nr=layers_nr,
                    num_classes=2)

        opt = torch.optim.Adam(model.parameters(), lr = 0.1)

        load_model = False
        if load_model:
            checkpoint = torch.load(model_path)
            model.load_state_dict(checkpoint['state_dict'])
            opt = checkpoint['optimizer']

        model.train()
        min_loss = 50

        best_model = GraphCheb(
                    num_node_features=input_dim,
                    hidden_channels=hidden_channels,
                    K=K,
                    layers_nr=1,
                    num_classes=2)

        min_val_loss = 1000000
        n_epochs_stop = 10
        epochs_no_improve = 0
        steps_per_epoch = 35

        for epoch in range(epoch_nr):
            model.train()
            pbar = tqdm(range(steps_per_epoch), unit='batch')
            epoch_loss = 0
            for pos in pbar:
                
                selected_idx = np.random.permutation(len(s2v_train_dataset))[:32]
                batch_graph_x = [s2v_train_dataset[idx] for idx in selected_idx]
                batch_graph = DataLoader(batch_graph_x, batch_size=32, shuffle=False)
                
                for batch_graph_y in batch_graph:
                    logits = model(batch_graph_y.x, batch_graph_y.edge_index, batch_graph_y.batch)

                labels = torch.LongTensor([graph.y for graph in batch_graph_x])
                if use_weights:
                    loss = nn.CrossEntropyLoss(weight=weight)(logits,labels)
                else:
                    loss = nn.CrossEntropyLoss()(logits,labels)

                opt.zero_grad()
                loss.backward()
                opt.step()

                epoch_loss += loss.detach().item()

            epoch_loss /= steps_per_epoch
            model.eval()
            
            tr = DataLoader(s2v_train_dataset, batch_size=len(s2v_train_dataset), shuffle=False)
            for vv in tr:
                output = model(vv.x, vv.edge_index, vv.batch)
            
            #output = pass_data_iteratively(model, s2v_train_dataset)
            predicted_class = output.max(1, keepdim=True)[1]
            labels = torch.LongTensor([graph.y for graph in s2v_train_dataset])
            correct = predicted_class.eq(labels.view_as(predicted_class)).sum().item()
            acc_train = correct / float(len(s2v_train_dataset))
            
            print('Epoch {}, loss {:.4f}'.format(epoch, epoch_loss))
            print(f"Train Acc {acc_train:.4f}")


            pbar.set_description('epoch: %d' % (epoch))
            val_loss = 0
            
            tr = DataLoader(s2v_test_dataset, batch_size=len(s2v_test_dataset), shuffle=False)
            for vv in tr:
                output = model(vv.x, vv.edge_index, vv.batch)

            #output = pass_data_iteratively(model, s2v_test_dataset)

            pred = output.max(1, keepdim=True)[1]
            labels = torch.LongTensor([graph.y for graph in s2v_test_dataset])
            if use_weights:
                    loss = nn.CrossEntropyLoss(weight=weight)(output,labels)
            else:
                loss = nn.CrossEntropyLoss()(output,labels)
            val_loss += loss

            print('Epoch {}, val_loss {:.4f}'.format(epoch, val_loss))
            if val_loss < min_val_loss:
                print(f"Saving best model with validation loss {val_loss}")
                best_model = copy.deepcopy(model)
                epochs_no_improve = 0
                min_val_loss = val_loss
                #if acc_train > 0.75:
                #    opt = torch.optim.Adam(model.parameters(), lr = 0.01)
                #if acc_train > 0.85:
                #    opt = torch.optim.Adam(model.parameters(), lr = 0.001)


            else:
                epochs_no_improve += 1
                # Check early stopping condition
                if epochs_no_improve == n_epochs_stop:
                    print('Early stopping!')
                    model.load_state_dict(best_model.state_dict())
                    break

        confusion_array = []
        true_class_array = []
        predicted_class_array = []
        model.eval()
        correct = 0
        true_class_array = []
        predicted_class_array = []

        test_loss = 0

        model.load_state_dict(best_model.state_dict())

        tr = DataLoader(s2v_test_dataset, batch_size=len(s2v_test_dataset), shuffle=False)
        for vv in tr:
            output = model(vv.x, vv.edge_index, vv.batch)

        #output = pass_data_iteratively(model, s2v_test_dataset)
        output = np.array(output.detach())
        predicted_class = output.argmax(1, keepdims=True)

        predicted_class = list(predicted_class)
        
        labels = torch.LongTensor([graph.y for graph in s2v_test_dataset])
        correct = torch.tensor(np.array(predicted_class)).eq(labels.view_as(torch.tensor(np.array(predicted_class)))).sum().item()

        confusion_matrix_gnn = confusion_matrix(labels, predicted_class)
        print("\nConfusion matrix (Validation set):\n")
        print(confusion_matrix_gnn)

        from sklearn.metrics import balanced_accuracy_score
        acc_bal = balanced_accuracy_score(labels, predicted_class)

        print("Validation accuracy: {}".format(acc_bal))

        model.train()

        self.model_status = 'Trained'
        self.model = copy.deepcopy(model)
        self.accuracy = acc_bal
        self.confusion_matrix = confusion_matrix_gnn
        #self.test_loss = test_loss
        self.s2v_test_dataset = s2v_test_dataset
        self.predictions = predicted_class_array
        self.true_class  = labels



    def train_chebconv(self, epoch_nr = 20, shuffle=True, weights=False):
        """
        Train the GNN model on the data provided during initialisation.
        """
        use_weights = False

        dataset = self.dataset
        gene_names = self.gene_names

        graphs_class_0_list = []
        graphs_class_1_list = []
        for graph in dataset:
            if graph.y.numpy() == 0:
                graphs_class_0_list.append(graph)
            else:
                graphs_class_1_list.append(graph)

        graphs_class_0_len = len(graphs_class_0_list)
        graphs_class_1_len = len(graphs_class_1_list)

        print(f"Graphs class 0: {graphs_class_0_len}, Graphs class 1: {graphs_class_1_len}")

        ########################################################################################################################
        # Downsampling of the class that contains more elements ===========================================================
        # ########################################################################################################################

        if graphs_class_0_len >= graphs_class_1_len:
            random_graphs_class_0_list = random.sample(graphs_class_0_list, graphs_class_1_len)
            balanced_dataset_list = graphs_class_1_list + random_graphs_class_0_list

        if graphs_class_0_len < graphs_class_1_len:
            random_graphs_class_1_list = random.sample(graphs_class_1_list, graphs_class_0_len)
            balanced_dataset_list = graphs_class_0_list + random_graphs_class_1_list

        random.shuffle(balanced_dataset_list)
        print(f"Length of balanced dataset list: {len(balanced_dataset_list)}")

        list_len = len(balanced_dataset_list)
        #print(list_len)
        train_set_len = int(list_len * 4 / 5)
        train_dataset_list = balanced_dataset_list[:train_set_len]
        test_dataset_list  = balanced_dataset_list[train_set_len:]

        train_graph_class_0_nr = 0
        train_graph_class_1_nr = 0
        for graph in train_dataset_list:
            if graph.y.numpy() == 0:
                train_graph_class_0_nr += 1
            else:
                train_graph_class_1_nr += 1
        print(f"Train graph class 0: {train_graph_class_0_nr}, train graph class 1: {train_graph_class_1_nr}")

        test_graph_class_0_nr = 0
        test_graph_class_1_nr = 0
        for graph in test_dataset_list:
            if graph.y.numpy() == 0:
                test_graph_class_0_nr += 1
            else:
                test_graph_class_1_nr += 1
        print(f"Validation graph class 0: {test_graph_class_0_nr}, validation graph class 1: {test_graph_class_1_nr}")

        # for ChebConv()
        s2v_train_dataset = train_dataset_list
        s2v_test_dataset  = test_dataset_list

        model_path = 'omics_model.pth'
        no_of_features = dataset[0].x.shape[1]
        nodes_per_graph_nr = dataset[0].x.shape[0]

        input_dim = no_of_features
        n_classes = 2

        #model = GraphCNN(num_layers, num_mlp_layers, input_dim, 32, n_classes, 0.5, True, graph_pooling_type, neighbor_pooling_type, 0)
        model = ChebConv(input_dim, n_classes, 10)
        
        #model = nn.Sequential(gat_layer,  ChebConv(input_dim, n_classes, 10)  )

        opt = torch.optim.Adam(model.parameters(), lr = 0.1)

        load_model = False
        if load_model:
            checkpoint = torch.load(model_path)
            model.load_state_dict(checkpoint['state_dict'])
            opt = checkpoint['optimizer']

        model.train()

        #min_loss = 50000
        #best_model = GraphCNN(num_layers, num_mlp_layers, input_dim, 32, n_classes, 0.5, True, graph_pooling_type, neighbor_pooling_type, 0)
        best_model = ChebConv(input_dim, n_classes, 10)

        min_val_loss = 1000000
        n_epochs_stop = 7
        epochs_no_improve = 0
        steps_per_epoch = 35
        
        for epoch in range(epoch_nr):
            model.train()
            pbar = tqdm(range(steps_per_epoch), unit='batch')
            epoch_loss = 0
            for pos in pbar:

                selected_idx = np.random.permutation(len(s2v_train_dataset))[:30]
                batch_graph = [s2v_train_dataset[idx] for idx in selected_idx]
                
                
                logits=[]
                for g in batch_graph:
                    logits.append(model(x=g.x, edge_index=g.edge_index).max(0)[0])
                    #logits.append(model(x=g.x, edge_index=g.edge_index).mean(0))

                logits = torch.reshape(torch.cat(logits,0),(30,2))  
                
                labels = torch.LongTensor([graph.y for graph in batch_graph])
                

                if use_weights:
                    loss = nn.CrossEntropyLoss(weight=weight)(logits,labels)
                else:
                    loss = nn.CrossEntropyLoss()(logits,labels)

                opt.zero_grad()
                loss.backward()
                opt.step()

                epoch_loss += loss.detach().item()

            epoch_loss /= steps_per_epoch
            model.eval()
            
            output = []
            for graphs in s2v_train_dataset: 
                output.append(model(x=graphs.x, edge_index=graphs.edge_index).max(0)[0])
                #output.append(model(x=graphs.x, edge_index=graphs.edge_index).mean(0))
                     
            output = torch.reshape(torch.cat(output,0),(len(output),2))
            
            output = np.array(output.detach())
            
            predicted_class = output.argmax(1, keepdims=True)          
            
            predicted_class = list(predicted_class)

            labels = torch.LongTensor([graph.y for graph in s2v_train_dataset])
            
            correct = torch.tensor(np.array(predicted_class)).eq(labels.view_as(torch.tensor(np.array(predicted_class)))).sum().item()

            acc_train = correct / len(s2v_train_dataset)
            print('Epoch {}, loss {:.4f}'.format(epoch, epoch_loss))
            print(f"Train Acc {acc_train:.4f}")
           

            pbar.set_description('epoch: %d' % (epoch))
            val_loss = 0

            output = []
            for graphs in s2v_test_dataset: 
                output.append(model(x=graphs.x, edge_index=graphs.edge_index).max(0)[0])
                #output.append(model(x=graphs.x, edge_index=graphs.edge_index).mean(0))

            output = torch.reshape(torch.cat(output,0),(len(output),2))

            labels = torch.LongTensor([graph.y for graph in s2v_test_dataset])
            
            if use_weights:
                loss = nn.CrossEntropyLoss(weight=weight)(output,labels)
            else:
                loss = nn.CrossEntropyLoss()(output,labels)
            val_loss += loss

            print('Epoch {}, val_loss {:.4f}'.format(epoch, val_loss))
            if val_loss < min_val_loss:
                print(f"Saving best model with validation loss {val_loss}")
                best_model = copy.deepcopy(model)
                epochs_no_improve = 0
                min_val_loss = val_loss

            else:
                epochs_no_improve += 1
                # Check early stopping condition
                if epochs_no_improve == n_epochs_stop:
                    print('Early stopping!')
                    model.load_state_dict(best_model.state_dict())
                    break

        confusion_array = []
        true_class_array = []
        predicted_class_array = []
        model.eval()
        correct = 0
        true_class_array = []
        predicted_class_array = []

        test_loss = 0

        model.load_state_dict(best_model.state_dict())

        output = []
        for graphs in s2v_test_dataset: 
            output.append(model(x=graphs.x, edge_index=graphs.edge_index).max(0)[0])
            #output.append(model(x=graphs.x, edge_index=graphs.edge_index).mean(0))
            
          
        output = torch.reshape(torch.cat(output,0),(len(output),2))
        output = np.array(output.detach())
        predicted_class = output.argmax(1, keepdims=True)

        predicted_class = list(predicted_class)
        
        labels = torch.LongTensor([graph.y for graph in s2v_test_dataset])
        
        correct = torch.tensor(np.array(predicted_class)).eq(labels.view_as(torch.tensor(np.array(predicted_class)))).sum().item()

        confusion_matrix_gnn = confusion_matrix(labels, predicted_class)
        print("\nConfusion matrix (Validation set):\n")
        print(confusion_matrix_gnn)

        from sklearn.metrics import balanced_accuracy_score
        acc_bal = balanced_accuracy_score(labels, predicted_class)

        print("Validation accuracy: {}".format(acc_bal))

        model.train()

        self.model_status = 'Trained'
        self.model = copy.deepcopy(model)
        self.accuracy = acc_bal
        self.confusion_matrix = confusion_matrix_gnn
        #self.test_loss = test_loss
        self.s2v_test_dataset = s2v_test_dataset
        self.predictions = predicted_class_array
        self.true_class  = labels

    #model = GraphCNN(5, 2, input_dim, 32, n_classes, 0.5, True, 'sum1', 'sum', 0)
    def train_graphcnn(self, num_layers=2, num_mlp_layers=2, epoch_nr = 20, shuffle=True, weights=False, graph_pooling_type='sum1', neighbor_pooling_type ='sum', learning_rate=0.1):
        """
        Train the GNN model on the data provided during initialisation.
        num_layers: number of layers in the neural networks (INCLUDING the input layer)
        num_mlp_layers: number of layers in mlps (EXCLUDING the input layer)
        graph_pooling_type: how to aggregate entire nodes in a graph (mean, average)
        neighbor_pooling_type: *sum*! how to aggregate neighbors (mean, average, or max)
        """
        use_weights = False

        dataset = self.dataset
        gene_names = self.gene_names

        graphs_class_0_list = []
        graphs_class_1_list = []
        for graph in dataset:
            if graph.y.numpy() == 0:
                graphs_class_0_list.append(graph)
            else:
                graphs_class_1_list.append(graph)

        graphs_class_0_len = len(graphs_class_0_list)
        graphs_class_1_len = len(graphs_class_1_list)

        print(f"Graphs class 0: {graphs_class_0_len}, Graphs class 1: {graphs_class_1_len}")

        ########################################################################################################################
        # Downsampling of the class that contains more elements ===========================================================
        # ########################################################################################################################

        if graphs_class_0_len >= graphs_class_1_len:
            random_graphs_class_0_list = random.sample(graphs_class_0_list, graphs_class_1_len)
            balanced_dataset_list = graphs_class_1_list + random_graphs_class_0_list

        if graphs_class_0_len < graphs_class_1_len:
            random_graphs_class_1_list = random.sample(graphs_class_1_list, graphs_class_0_len)
            balanced_dataset_list = graphs_class_0_list + random_graphs_class_1_list

        #print(len(random_graphs_class_0_list))
        #print(len(random_graphs_class_1_list))

        random.shuffle(balanced_dataset_list)
        print(f"Length of balanced dataset list: {len(balanced_dataset_list)}")

        list_len = len(balanced_dataset_list)
        #print(list_len)
        train_set_len = int(list_len * 4 / 5)
        train_dataset_list = balanced_dataset_list[:train_set_len]
        test_dataset_list  = balanced_dataset_list[train_set_len:]

        train_graph_class_0_nr = 0
        train_graph_class_1_nr = 0
        for graph in train_dataset_list:
            if graph.y.numpy() == 0:
                train_graph_class_0_nr += 1
            else:
                train_graph_class_1_nr += 1
        print(f"Train graph class 0: {train_graph_class_0_nr}, train graph class 1: {train_graph_class_1_nr}")

        test_graph_class_0_nr = 0
        test_graph_class_1_nr = 0
        for graph in test_dataset_list:
            if graph.y.numpy() == 0:
                test_graph_class_0_nr += 1
            else:
                test_graph_class_1_nr += 1
        print(f"Validation graph class 0: {test_graph_class_0_nr}, validation graph class 1: {test_graph_class_1_nr}")

        s2v_train_dataset = convert_to_s2vgraph(train_dataset_list)
        s2v_test_dataset  = convert_to_s2vgraph(test_dataset_list)


        # TRAIN GNN -------------------------------------------------- #
        #count = 0
        #for item in dataset:
        #    count += item.y.item()

        #weight = torch.tensor([count/len(dataset), 1-count/len(dataset)])
        #print(count/len(dataset), 1-count/len(dataset))

        model_path = 'omics_model.pth'
        no_of_features = dataset[0].x.shape[1]
        nodes_per_graph_nr = dataset[0].x.shape[0]

        #print(len(dataset), len(dataset)*0.2)
        #s2v_dataset = convert_to_s2vgraph(dataset)
        #train_dataset, test_dataset = train_test_split(dataset, test_size=0.2, random_state=123)
        #s2v_train_dataset = convert_to_s2vgraph(train_dataset)
        #s2v_test_dataset = convert_to_s2vgraph(test_dataset)
        #s2v_train_dataset, s2v_test_dataset = train_test_split(s2v_dataset, test_size=0.2, random_state=123)

        input_dim = no_of_features
        n_classes = 2

        model = GraphCNN(num_layers, num_mlp_layers, input_dim, 32, n_classes, 0.5, True, graph_pooling_type, neighbor_pooling_type, 0)
        opt = torch.optim.Adam(model.parameters(), lr = learning_rate)

        load_model = False
        if load_model:
            checkpoint = torch.load(model_path)
            model.load_state_dict(checkpoint['state_dict'])
            opt = checkpoint['optimizer']

        model.train()
        min_loss = 50
        best_model = GraphCNN(num_layers, num_mlp_layers, input_dim, 32, n_classes, 0.5, True, graph_pooling_type, neighbor_pooling_type, 0)
        min_val_loss = 1000000
        n_epochs_stop = 10
        epochs_no_improve = 0
        steps_per_epoch = 35

        for epoch in range(epoch_nr):
            model.train()
            pbar = tqdm(range(steps_per_epoch), unit='batch')
            epoch_loss = 0
            for pos in pbar:
                selected_idx = np.random.permutation(len(s2v_train_dataset))[:32]

                batch_graph = [s2v_train_dataset[idx] for idx in selected_idx]
                logits = model(batch_graph)
                labels = torch.LongTensor([graph.label for graph in batch_graph])
                if use_weights:
                    loss = nn.CrossEntropyLoss(weight=weight)(logits,labels)
                else:
                    loss = nn.CrossEntropyLoss()(logits,labels)

                opt.zero_grad()
                loss.backward()
                opt.step()

                epoch_loss += loss.detach().item()

            epoch_loss /= steps_per_epoch
            model.eval()
            output = pass_data_iteratively(model, s2v_train_dataset)
            predicted_class = output.max(1, keepdim=True)[1]
            labels = torch.LongTensor([graph.label for graph in s2v_train_dataset])
            correct = predicted_class.eq(labels.view_as(predicted_class)).sum().item()
            acc_train = correct / float(len(s2v_train_dataset))
            print('Epoch {}, loss {:.4f}'.format(epoch, epoch_loss))
            print(f"Train Acc {acc_train:.4f}")


            pbar.set_description('epoch: %d' % (epoch))
            val_loss = 0
            output = pass_data_iteratively(model, s2v_test_dataset)

            pred = output.max(1, keepdim=True)[1]
            labels = torch.LongTensor([graph.label for graph in s2v_test_dataset])
            if use_weights:
                    loss = nn.CrossEntropyLoss(weight=weight)(output,labels)
            else:
                loss = nn.CrossEntropyLoss()(output,labels)
            val_loss += loss

            print('Epoch {}, val_loss {:.4f}'.format(epoch, val_loss))
            if val_loss < min_val_loss:
                print(f"Saving best model with validation loss {val_loss}")
                best_model = copy.deepcopy(model)
                epochs_no_improve = 0
                min_val_loss = val_loss

            else:
                epochs_no_improve += 1
                # Check early stopping condition
                if epochs_no_improve == n_epochs_stop:
                    print('Early stopping!')
                    model.load_state_dict(best_model.state_dict())
                    break

        confusion_array = []
        true_class_array = []
        predicted_class_array = []
        model.eval()
        correct = 0
        true_class_array = []
        predicted_class_array = []

        test_loss = 0

        model.load_state_dict(best_model.state_dict())

        output = pass_data_iteratively(model, s2v_test_dataset)
        predicted_class = output.max(1, keepdim=True)[1]
        labels = torch.LongTensor([graph.label for graph in s2v_test_dataset])
        correct = predicted_class.eq(labels.view_as(predicted_class)).sum().item()
        acc_test = correct / float(len(s2v_test_dataset))

        if use_weights:
            loss = nn.CrossEntropyLoss(weight=weight)(output,labels)
        else:
            loss = nn.CrossEntropyLoss()(output,labels)
        test_loss = loss

        predicted_class_array = np.append(predicted_class_array, predicted_class)
        true_class_array = np.append(true_class_array, labels)

        confusion_matrix_gnn = confusion_matrix(true_class_array, predicted_class_array)
        print("\nConfusion matrix (Validation set):\n")
        print(confusion_matrix_gnn)


        counter = 0
        for it, i in zip(predicted_class_array, range(len(predicted_class_array))):
            if it == true_class_array[i]:
                counter += 1

        accuracy = counter/len(true_class_array) * 100
        print("Validation accuracy: {}%".format(accuracy))
        print("Validation loss {}".format(test_loss))

        checkpoint = {
            'state_dict': best_model.state_dict(),
            'optimizer': opt.state_dict()
        }
        torch.save(checkpoint, model_path)

        model.train()

        self.model_status = 'Trained'
        self.model = copy.deepcopy(model)
        self.accuracy = accuracy
        self.confusion_matrix = confusion_matrix_gnn
        self.test_loss = test_loss
        self.s2v_test_dataset = s2v_test_dataset
        self.predictions = predicted_class_array
        self.true_class  = true_class_array

    def explain_graphcheb(self, n_runs=10, explainer_lambda=0.8, communities=True, save_to_disk=False):
        """
        Explain the model's results.
        """

        ############################################
        # Run the Explainer
        ############################################

        LOC = self.location
        model = self.model
        s2v_test_dataset = self.s2v_test_dataset
        dataset = self.dataset
        gene_names = self.gene_names

        print("")
        print("------- Run the Explainer -------")
        print("")

        no_of_runs = n_runs
        lamda = 0.8 # not used!
        ems = []
        NODE_MASK = list()

        for idx in range(no_of_runs):
            print(f'Explainer::Iteration {idx+1} of {no_of_runs}')
            exp = GNNExplainer(model, epochs=300)
            em = exp.explain_graph_modified_cheb2(s2v_test_dataset, lamda)
            #Path(f"{path}/{sigma}/modified_gnn").mkdir(parents=True, exist_ok=True)
            gnn_feature_masks = np.reshape(em, (len(em), -1))
            NODE_MASK.append(np.array(gnn_feature_masks.sigmoid()))
            np.savetxt(f'{LOC}/gnn_feature_masks{idx}.csv', gnn_feature_masks.sigmoid(), delimiter=',', fmt='%.3f')
            #np.savetxt(f'{path}/{sigma}/modified_gnn/gnn_feature_masks{idx}.csv', gnn_feature_masks.sigmoid(), delimiter=',', fmt='%.3f')
            gnn_edge_masks = calc_edge_importance(gnn_feature_masks, dataset[0].edge_index)
            np.savetxt(f'{LOC}/gnn_edge_masks{idx}.csv', gnn_edge_masks.sigmoid(), delimiter=',', fmt='%.3f')
            #np.savetxt(f'{path}/{sigma}/modified_gnn/gnn_edge_masks{idx}.csv', gnn_edge_masks.sigmoid(), delimiter=',', fmt='%.3f')
            ems.append(gnn_edge_masks.sigmoid().numpy())

        ems     = np.array(ems)
        mean_em = ems.mean(0)

        # OUTPUT -- Save Edge Masks
        np.savetxt(f'{LOC}/edge_masks.txt', mean_em, delimiter=',', fmt='%.5f')
        self.edge_mask = mean_em
        self.node_mask_matrix = np.concatenate(NODE_MASK,1)
        self.node_mask = np.concatenate(NODE_MASK,1).mean(1)

        self._explainer_run = True
        
        ###############################################
        # Perform Community Detection
        ###############################################

        if communities:
            avg_mask, coms = find_communities(f'{LOC}/edge_index.txt', f'{LOC}/edge_masks.txt')
            self.modules = coms
            self.module_importances = avg_mask

            np.savetxt(f'{LOC}/communities_scores.txt', avg_mask, delimiter=',', fmt='%.3f')

            filePath = f'{LOC}/communities.txt'

            if os.path.exists(filePath):
                os.remove(filePath)

            f = open(f'{LOC}/communities.txt', "a")
            for idx in range(len(avg_mask)):
                s_com = ','.join(str(e) for e in coms[idx])
                f.write(s_com + '\n')

            f.close()

            # Write gene_names to file
            textfile = open(f'{LOC}/gene_names.txt', "w")
            for element in gene_names:
                listToStr = ''.join(map(str, element))
                textfile.write(listToStr + "\n")

            textfile.close()

        self._explainer_run = True    
    
    
    def explain_chebconv(self, n_runs=10, explainer_lambda=0.8, communities=True, save_to_disk=False):
        """
        Explain the model's results.
        """

        ############################################
        # Run the Explainer
        ############################################

        LOC = self.location
        model = self.model
        s2v_test_dataset = self.s2v_test_dataset
        dataset = self.dataset
        gene_names = self.gene_names

        print("")
        print("------- Run the Explainer -------")
        print("")

        no_of_runs = n_runs
        lamda = 0.8 # not used!
        ems = []
        NODE_MASK = list()

        for idx in range(no_of_runs):
            print(f'Explainer::Iteration {idx+1} of {no_of_runs}')
            exp = GNNExplainer(model, epochs=300)
            em = exp.explain_graph_modified_cheb(s2v_test_dataset, lamda)
            #Path(f"{path}/{sigma}/modified_gnn").mkdir(parents=True, exist_ok=True)
            gnn_feature_masks = np.reshape(em, (len(em), -1))
            NODE_MASK.append(np.array(gnn_feature_masks.sigmoid()))
            np.savetxt(f'{LOC}/gnn_feature_masks{idx}.csv', gnn_feature_masks.sigmoid(), delimiter=',', fmt='%.3f')
            #np.savetxt(f'{path}/{sigma}/modified_gnn/gnn_feature_masks{idx}.csv', gnn_feature_masks.sigmoid(), delimiter=',', fmt='%.3f')
            gnn_edge_masks = calc_edge_importance(gnn_feature_masks, dataset[0].edge_index)
            np.savetxt(f'{LOC}/gnn_edge_masks{idx}.csv', gnn_edge_masks.sigmoid(), delimiter=',', fmt='%.3f')
            #np.savetxt(f'{path}/{sigma}/modified_gnn/gnn_edge_masks{idx}.csv', gnn_edge_masks.sigmoid(), delimiter=',', fmt='%.3f')
            ems.append(gnn_edge_masks.sigmoid().numpy())

        ems     = np.array(ems)
        mean_em = ems.mean(0)

        # OUTPUT -- Save Edge Masks
        np.savetxt(f'{LOC}/edge_masks.txt', mean_em, delimiter=',', fmt='%.5f')
        self.edge_mask = mean_em
        self.node_mask_matrix = np.concatenate(NODE_MASK,1)
        self.node_mask = np.concatenate(NODE_MASK,1).mean(1)

        self._explainer_run = True
        
        ###############################################
        # Perform Community Detection
        ###############################################

        if communities:
            avg_mask, coms = find_communities(f'{LOC}/edge_index.txt', f'{LOC}/edge_masks.txt')
            self.modules = coms
            self.module_importances = avg_mask

            np.savetxt(f'{LOC}/communities_scores.txt', avg_mask, delimiter=',', fmt='%.3f')

            filePath = f'{LOC}/communities.txt'

            if os.path.exists(filePath):
                os.remove(filePath)

            f = open(f'{LOC}/communities.txt', "a")
            for idx in range(len(avg_mask)):
                s_com = ','.join(str(e) for e in coms[idx])
                f.write(s_com + '\n')

            f.close()

            # Write gene_names to file
            textfile = open(f'{LOC}/gene_names.txt', "w")
            for element in gene_names:
                listToStr = ''.join(map(str, element))
                textfile.write(listToStr + "\n")

            textfile.close()

        self._explainer_run = True    

    def explain_graphcnn(self, n_runs=10, explainer_lambda=0.8, communities=True, save_to_disk=False):
        """
        Explain the model's results.
        """

        ############################################
        # Run the Explainer
        ############################################

        LOC = self.location
        model = self.model
        s2v_test_dataset = self.s2v_test_dataset
        dataset = self.dataset
        gene_names = self.gene_names

        print("")
        print("------- Run the Explainer -------")
        print("")

        no_of_runs = n_runs
        lamda = 0.8 # not used!
        ems = []
        NODE_MASK = list()

        for idx in range(no_of_runs):
            print(f'Explainer::Iteration {idx+1} of {no_of_runs}')
            exp = GNNExplainer(model, epochs=300)
            em = exp.explain_graph_modified_s2v(s2v_test_dataset, lamda)
            #Path(f"{path}/{sigma}/modified_gnn").mkdir(parents=True, exist_ok=True)
            gnn_feature_masks = np.reshape(em, (len(em), -1))
            NODE_MASK.append(np.array(gnn_feature_masks.sigmoid()))
            np.savetxt(f'{LOC}/gnn_feature_masks{idx}.csv', gnn_feature_masks.sigmoid(), delimiter=',', fmt='%.3f')
            #np.savetxt(f'{path}/{sigma}/modified_gnn/gnn_feature_masks{idx}.csv', gnn_feature_masks.sigmoid(), delimiter=',', fmt='%.3f')
            gnn_edge_masks = calc_edge_importance(gnn_feature_masks, dataset[0].edge_index)
            np.savetxt(f'{LOC}/gnn_edge_masks{idx}.csv', gnn_edge_masks.sigmoid(), delimiter=',', fmt='%.3f')
            #np.savetxt(f'{path}/{sigma}/modified_gnn/gnn_edge_masks{idx}.csv', gnn_edge_masks.sigmoid(), delimiter=',', fmt='%.3f')
            ems.append(gnn_edge_masks.sigmoid().numpy())

        ems     = np.array(ems)
        mean_em = ems.mean(0)

        # OUTPUT -- Save Edge Masks
        np.savetxt(f'{LOC}/edge_masks.txt', mean_em, delimiter=',', fmt='%.5f')
        self.edge_mask = mean_em
        self.node_mask_matrix = np.concatenate(NODE_MASK,1)
        self.node_mask = np.concatenate(NODE_MASK,1).mean(1)

        self._explainer_run = True
        
        ###############################################
        # Perform Community Detection
        ###############################################

        if communities:
            avg_mask, coms = find_communities(f'{LOC}/edge_index.txt', f'{LOC}/edge_masks.txt')
            self.modules = coms
            self.module_importances = avg_mask

            np.savetxt(f'{LOC}/communities_scores.txt', avg_mask, delimiter=',', fmt='%.3f')

            filePath = f'{LOC}/communities.txt'

            if os.path.exists(filePath):
                os.remove(filePath)

            f = open(f'{LOC}/communities.txt', "a")
            for idx in range(len(avg_mask)):
                s_com = ','.join(str(e) for e in coms[idx])
                f.write(s_com + '\n')

            f.close()

            # Write gene_names to file
            textfile = open(f'{LOC}/gene_names.txt', "w")
            for element in gene_names:
                listToStr = ''.join(map(str, element))
                textfile.write(listToStr + "\n")

            textfile.close()

        self._explainer_run = True

    def predict_graphcheb(self, gnnsubnet_test):

        confusion_array = []
        true_class_array = []
        predicted_class_array = []

        s2v_test_dataset  = gnnsubnet_test.dataset
        model = self.model
        model.eval()

        tr = DataLoader(s2v_test_dataset, batch_size=len(s2v_test_dataset), shuffle=False)
        for vv in tr:
            output = model(vv.x, vv.edge_index, vv.batch)

        output = np.array(output.detach())
        predicted_class = output.argmax(1, keepdims=True)

        predicted_class = list(predicted_class)
        
        labels = torch.LongTensor([graph.y for graph in s2v_test_dataset])
        
        correct = torch.tensor(np.array(predicted_class)).eq(labels.view_as(torch.tensor(np.array(predicted_class)))).sum().item()

        confusion_matrix_gnn = confusion_matrix(labels, predicted_class)
        print("\nConfusion matrix (Validation set):\n")
        print(confusion_matrix_gnn)

        from sklearn.metrics import balanced_accuracy_score
        acc_bal = balanced_accuracy_score(labels, predicted_class)

        print("Validation accuracy: {}".format(acc_bal))
        
        self.predictions_test = predicted_class
        self.true_class_test  = labels
        self.accuracy_test = acc_bal
        self.confusion_matrix_test = confusion_matrix_gnn
        
        return predicted_class


    def predict_chebconv(self, gnnsubnet_test):

        confusion_array = []
        true_class_array = []
        predicted_class_array = []

        s2v_test_dataset  = gnnsubnet_test.dataset
        model = self.model
        model.eval()

        output = []
        for graphs in s2v_test_dataset: 
            output.append(model(x=graphs.x, edge_index=graphs.edge_index).max(0)[0])
            #output.append(model(x=graphs.x, edge_index=graphs.edge_index).mean(0))
            
          
        output = torch.reshape(torch.cat(output,0),(len(output),2))
        output = np.array(output.detach())
        predicted_class = output.argmax(1, keepdims=True)

        predicted_class = list(predicted_class)
        
        labels = torch.LongTensor([graph.y for graph in s2v_test_dataset])
        
        correct = torch.tensor(np.array(predicted_class)).eq(labels.view_as(torch.tensor(np.array(predicted_class)))).sum().item()

        confusion_matrix_gnn = confusion_matrix(labels, predicted_class)
        print("\nConfusion matrix (Validation set):\n")
        print(confusion_matrix_gnn)

        from sklearn.metrics import balanced_accuracy_score
        acc_bal = balanced_accuracy_score(labels, predicted_class)

        print("Validation accuracy: {}".format(acc_bal))
        
        self.predictions_test = predicted_class
        self.true_class_test  = labels
        self.accuracy_test = acc_bal
        self.confusion_matrix_test = confusion_matrix_gnn
        
        return predicted_class
    
    def predict_graphcnn(self, gnnsubnet_test):

        confusion_array = []
        true_class_array = []
        predicted_class_array = []

        s2v_test_dataset  = convert_to_s2vgraph(gnnsubnet_test.dataset)
        model = self.model
        model.eval()
        output = pass_data_iteratively(model, s2v_test_dataset)
        predicted_class = output.max(1, keepdim=True)[1]
        labels = torch.LongTensor([graph.label for graph in s2v_test_dataset])
        correct = predicted_class.eq(labels.view_as(predicted_class)).sum().item()
        acc_test = correct / float(len(s2v_test_dataset))

        #if use_weights:
        #    loss = nn.CrossEntropyLoss(weight=weight)(output,labels)
        #else:
        #    loss = nn.CrossEntropyLoss()(output,labels)
        #test_loss = loss

        predicted_class_array = np.append(predicted_class_array, predicted_class)
        true_class_array = np.append(true_class_array, labels)

        confusion_matrix_gnn = confusion_matrix(true_class_array, predicted_class_array)
        print("\nConfusion matrix:\n")
        print(confusion_matrix_gnn)

        counter = 0
        for it, i in zip(predicted_class_array, range(len(predicted_class_array))):
            if it == true_class_array[i]:
                counter += 1

        accuracy = counter/len(true_class_array) * 100
        print("Accuracy: {}%".format(accuracy))
        
        self.predictions_test = predicted_class_array
        self.true_class_test  = true_class_array
        self.accuracy_test = accuracy
        self.confusion_matrix_test = confusion_matrix_gnn
        
        return predicted_class_array

    def download_TCGA(self, save_to_disk=False) -> None:
        """
        Warning: Currently not implemented!

        Download some sample TCGA data. Running this function will download
        approximately 100MB of data.
        """
        base_url = 'https://raw.githubusercontent.com/pievos101/GNN-SubNet/python-package/TCGA/' # CHANGE THIS URL WHEN BRANCH MERGES TO MAIN

        KIDNEY_RANDOM_Methy_FEATURES_filename = 'KIDNEY_RANDOM_Methy_FEATURES.txt'
        KIDNEY_RANDOM_PPI_filename = 'KIDNEY_RANDOM_PPI.txt'
        KIDNEY_RANDOM_TARGET_filename = 'KIDNEY_RANDOM_TARGET.txt'
        KIDNEY_RANDOM_mRNA_FEATURES_filename = 'KIDNEY_RANDOM_mRNA_FEATURES.txt'

        # For testing let's use KIDNEY_RANDOM_Methy_FEATURES and store in memory.
        raw = requests.get(base_url + KIDNEY_RANDOM_Methy_FEATURES_filename, stream=True)

        self.KIDNEY_RANDOM_Methy_FEATURES = np.asarray(pd.read_csv(io.BytesIO(raw.content), delimiter=' '))

        # Clear some memory
        raw = None

        return None
