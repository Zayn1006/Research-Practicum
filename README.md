# Installation: 
pip install GNNSubNet\

# Example: 
from GNNSubNet import GNNSubNet as gnn\

#location of the file.\
loc   = "/Users/jasonliu/GNN-SubNet/TCGA"\

#PPI network\
ppi   = f'{loc}/KIDNEY_RANDOM_PPI.txt'\

#single-omic features\
#feats = [f'{loc}/KIDNEY_RANDOM_Methy_FEATURES.txt']\

#multi-omic features\
feats = [f'{loc}/KIDNEY_RANDOM_mRNA_FEATURES.txt', f'{loc}/KIDNEY_RANDOM_Methy_FEATURES.txt']\

#outcome class\
targ  = f'{loc}/KIDNEY_RANDOM_TARGET.txt'\

#Load the multi-omics data\
g = gnn.GNNSubNet(loc, ppi, feats, targ, random_seed=42)\

#Train the "chebconv" GNN classifier and validate performance on a test set with Attention mechanism\
g.train(method="chebconv", use_attention=True)\
