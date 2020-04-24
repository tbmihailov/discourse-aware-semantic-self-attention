from docqa.allennlp_custom.modules.similarity_functions.linear_extended_feedforward import LinearExtendedFeedForwardReprCombination
from docqa.allennlp_custom.modules.similarity_functions.linear_extended import LinearExtenedSimilarity
from docqa.allennlp_custom.modules.similarity_functions.linear_transform_sum_repr_combination import LinearTransformSumReprCombination
from docqa.allennlp_custom.modules.similarity_functions.linear_transform_sum_repr_combination_tri import LinearTransformSumReprCombinationTriParams
from docqa.allennlp_custom.modules.similarity_functions.weighted_sum_repr_combination import WeightedSumReprCombination
from docqa.allennlp_custom.modules.similarity_functions.constant_tri import ConstantTriParams

from docqa.allennlp_custom.utils.tokenizers.word_splitter import ReadSentenceParseTypeWise
from docqa.allennlp_custom.modules.seq2seq_encoders.bidaf_encoder import BidafInteractionEncoder
from docqa.allennlp_custom.modules.seq2seq_encoders.qanet_semantic_encoder import QaNetSemanticEncoder
from docqa.allennlp_custom.modules.seq2seq_encoders.multi_head_semantic_self_attention import MultiHeadSemanticSelfAttention
from docqa.allennlp_custom.modules.seq2seq_encoders.qanet_semantic_flat_encoder import QaNetSemanticFlatEncoder
from docqa.allennlp_custom.modules.seq2seq_encoders.multi_head_semantic_flat_self_attention import MultiHeadSemanticFlatSelfAttention

from docqa.allennlp_custom.modules.seq2seq_encoders.qanet_semantic_flat_concat_encoder import QaNetSemanticFlatConcatEncoder
from docqa.allennlp_custom.modules.seq2seq_encoders.multi_head_semantic_flat_concat_self_attention import MultiHeadSemanticFlatConcatSelfAttention

from docqa.allennlp_custom.training.trainer_grad_accum import TrainerGradientAccumulation
