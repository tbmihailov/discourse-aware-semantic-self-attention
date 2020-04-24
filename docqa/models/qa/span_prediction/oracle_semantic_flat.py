import logging

import rouge
from typing import Any, Dict, List, Optional

import torch
from torch.nn.functional import nll_loss

from allennlp.data import Vocabulary
from allennlp.models.model import Model
from allennlp.models.reading_comprehension.util import get_best_span
from allennlp.modules import Highway
from allennlp.modules import Seq2SeqEncoder, TextFieldEmbedder
from allennlp.modules.matrix_attention.matrix_attention import MatrixAttention
from allennlp.nn import util, InitializerApplicator, RegularizerApplicator
from allennlp.training.metrics import BooleanAccuracy, CategoricalAccuracy, SquadEmAndF1, Average
from allennlp.nn.util import masked_softmax

from allennlp.tools import squad_eval
from docqa.allennlp_custom import QaNetSemanticEncoder, QaNetSemanticFlatEncoder, QaNetSemanticFlatConcatEncoder
from docqa.allennlp_custom.training.metrics.squad_em_and_f1_custom import SquadEmAndF1Custom
from docqa.allennlp_custom.utils.common_utils import is_output_meta_supported
from docqa.nn.util import to_cuda


@Model.register("oracle_semantic_flat")
class OracleSemanticFlat(Model):
    """
    This class implements Adams Wei Yu's `QANet Model <https://openreview.net/forum?id=B14TlG-RW>`_
    for machine reading comprehension published at ICLR 2018.

    The overall architecture of QANet is very similar to BiDAF. The main difference is that QANet
    replaces the RNN encoder with CNN + self-attention. There are also some minor differences in the
    modeling layer and output layer.

    Parameters
    ----------
    vocab : ``Vocabulary``
    text_field_embedder : ``TextFieldEmbedder``
        Used to embed the ``question`` and ``passage`` ``TextFields`` we get as input to the model.
    num_highway_layers : ``int``
        The number of highway layers to use in between embedding the input and passing it through
        the phrase layer.
    phrase_layer : ``Seq2SeqEncoder``
        The encoder (with its own internal stacking) that we will use in between embedding tokens
        and doing the passage-question attention.
    matrix_attention_layer : ``MatrixAttention``
        The matrix attention function that we will use when comparing encoded passage and question
        representations.
    modeling_layer : ``Seq2SeqEncoder``
        The encoder (with its own internal stacking) that we will use in between the bidirectional
        attention and predicting span start and end.
    dropout_prob : ``float``, optional (default=0.1)
        If greater than 0, we will apply dropout with this probability between layers.
    initializer : ``InitializerApplicator``, optional (default=``InitializerApplicator()``)
        Used to initialize the model parameters.
    regularizer : ``RegularizerApplicator``, optional (default=``None``)
        If provided, will be used to calculate the regularization penalty during training.
    """
    def __init__(self, vocab: Vocabulary,
                 text_field_embedder: TextFieldEmbedder,
                 num_highway_layers: int,
                 phrase_layer: Seq2SeqEncoder,
                 matrix_attention_layer: MatrixAttention,
                 modeling_layer: Seq2SeqEncoder,
                 dropout_prob: float = 0.1,
                 use_semantic_views=True,
                 initializer: InitializerApplicator = InitializerApplicator(),
                 regularizer: Optional[RegularizerApplicator] = None) -> None:
        super().__init__(vocab, regularizer)

        text_embed_dim = text_field_embedder.get_output_dim()
        encoding_in_dim = phrase_layer.get_input_dim()
        encoding_out_dim = phrase_layer.get_output_dim()
        modeling_in_dim = modeling_layer.get_input_dim()
        modeling_out_dim = modeling_layer.get_output_dim()

        self.return_output_metadata = False

        self.use_semantic_views = use_semantic_views

        self._text_field_embedder = text_field_embedder

        self._embedding_proj_layer = torch.nn.Linear(text_embed_dim, encoding_in_dim)
        self._highway_layer = Highway(encoding_in_dim, num_highway_layers)

        self._encoding_proj_layer = torch.nn.Linear(encoding_in_dim, encoding_in_dim)
        self._phrase_layer = phrase_layer

        self._matrix_attention = matrix_attention_layer

        self._modeling_proj_layer = torch.nn.Linear(encoding_out_dim * 4, modeling_in_dim)
        self._modeling_layer = modeling_layer

        self._span_start_predictor = torch.nn.Linear(modeling_out_dim * 2, 1)
        self._span_end_predictor = torch.nn.Linear(modeling_out_dim * 2, 1)

        self._span_start_accuracy = CategoricalAccuracy()
        self._span_end_accuracy = CategoricalAccuracy()
        self._span_accuracy = BooleanAccuracy()

        self._squad_metrics = SquadEmAndF1Custom()
        self._dropout = torch.nn.Dropout(p=dropout_prob) if dropout_prob > 0 else lambda x: x

        # evaluation

        # BLEU
        self._bleu_score_types_to_use = ["BLEU1", "BLEU2", "BLEU3", "BLEU4"]
        self._bleu_scores = {x: Average() for x in self._bleu_score_types_to_use}

        # ROUGE using pyrouge
        self._rouge_score_types_to_use = ['rouge-n', 'rouge-l', 'rouge-w']

        # if we have rouge-n as metric we actualy get n scores like rouge-1, rouge-2, .., rouge-n
        max_rouge_n = 4
        rouge_n_metrics = []
        if "rouge-n" in self._rouge_score_types_to_use:
            rouge_n_metrics = ["rouge-{0}".format(x) for x in range(1, max_rouge_n + 1)]

        rouge_scores_names = rouge_n_metrics + [y for y in self._rouge_score_types_to_use if y != 'rouge-n']
        self._rouge_scores = {x: Average() for x in rouge_scores_names}
        self._rouge_evaluator = rouge.Rouge(metrics=self._rouge_score_types_to_use,
                                            max_n=max_rouge_n,
                                            limit_length=True,
                                            length_limit=100,
                                            length_limit_type='words',
                                            apply_avg=False,
                                            apply_best=False,
                                            alpha=0.5,  # Default F1_score
                                            weight_factor=1.2,
                                            stemming=True)

        initializer(self)


    def forward(self,  # type: ignore
                question: Dict[str, torch.LongTensor],
                passage: Dict[str, torch.LongTensor],
                span_start: torch.IntTensor = None,
                span_end: torch.IntTensor = None,
                passage_sem_views_q: torch.IntTensor = None,
                passage_sem_views_k: torch.IntTensor = None,
                question_sem_views_q: torch.IntTensor = None,
                question_sem_views_k: torch.IntTensor = None,
                metadata: List[Dict[str, Any]] = None
                ) -> Dict[str, torch.Tensor]:
        # pylint: disable=arguments-differ
        """
        Parameters
        ----------
        question : Dict[str, torch.LongTensor]
            From a ``TextField``.
        passage : Dict[str, torch.LongTensor]
            From a ``TextField``.  The model assumes that this passage contains the answer to the
            question, and predicts the beginning and ending positions of the answer within the
            passage.
        span_start : ``torch.IntTensor``, optional
            From an ``IndexField``.  This is one of the things we are trying to predict - the
            beginning position of the answer with the passage.  This is an `inclusive` token index.
            If this is given, we will compute a loss that gets included in the output dictionary.
        span_end : ``torch.IntTensor``, optional
            From an ``IndexField``.  This is one of the things we are trying to predict - the
            ending position of the answer with the passage.  This is an `inclusive` token index.
            If this is given, we will compute a loss that gets included in the output dictionary.
        passage_sem_views_q : ``torch.IntTensor``, optional
            Paragraph semantic views features for multihead attention Query (Q)
        passage_sem_views_k : ``torch.IntTensor``, optional
            Paragraph semantic views features for multihead attention Key (K)
        question_sem_views_q : ``torch.IntTensor``, optional
            Paragraph semantic views features for multihead attention Query (Q)
        question_sem_views_k : ``torch.IntTensor``, optional
            Paragraph semantic views features for multihead attention Key (K)

        metadata : ``List[Dict[str, Any]]``, optional
            If present, this should contain the question tokens, passage tokens, original passage
            text, and token offsets into the passage for each instance in the batch.  The length
            of this list should be the batch size, and each dictionary should have the keys
            ``question_tokens``, ``passage_tokens``, ``original_passage``, and ``token_offsets``.

        Returns
        -------
        An output dictionary consisting of:
        span_start_logits : torch.FloatTensor
            A tensor of shape ``(batch_size, passage_length)`` representing unnormalized log
            probabilities of the span start position.
        span_start_probs : torch.FloatTensor
            The result of ``softmax(span_start_logits)``.
        span_end_logits : torch.FloatTensor
            A tensor of shape ``(batch_size, passage_length)`` representing unnormalized log
            probabilities of the span end position (inclusive).
        span_end_probs : torch.FloatTensor
            The result of ``softmax(span_end_logits)``.
        best_span : torch.IntTensor
            The result of a constrained inference over ``span_start_logits`` and
            ``span_end_logits`` to find the most probable span.  Shape is ``(batch_size, 2)``
            and each offset is a token index.
        loss : torch.FloatTensor, optional
            A scalar loss to be optimised.
        best_span_str : List[str]
            If sufficient metadata was provided for the instances in the batch, we also return the
            string from the original passage that the model thinks is the best answer to the
            question.
        """

        return_output_metadata = self.return_output_metadata


        question_mask = util.get_text_field_mask(question).float()
        passage_mask = util.get_text_field_mask(passage).float()

        batch_size, passage_len = passage_mask.shape

        # convert to long
        if passage_sem_views_q is not None:
            passage_sem_views_q = passage_sem_views_q.long()

        if passage_sem_views_k is not None:
            passage_sem_views_k = passage_sem_views_k.long()

        if question_sem_views_q is not None:
            question_sem_views_q = question_sem_views_q.long()

        if question_sem_views_k is not None:
            question_sem_views_k = question_sem_views_k.long()

        span_start_logits = torch.FloatTensor(batch_size, passage_len)
        span_start_logits.zero_()
        span_start_logits.scatter_(1, span_start, 1)

        span_end_logits = torch.FloatTensor(batch_size, passage_len)
        span_end_logits.zero_()
        span_end_logits.scatter_(1, span_end, 1)

        span_start_logits = util.replace_masked_values(span_start_logits, passage_mask, -1e32)
        span_end_logits = util.replace_masked_values(span_end_logits, passage_mask, -1e32)

        # Shape: (batch_size, passage_length)
        span_start_probs = torch.nn.functional.softmax(span_start_logits, dim=-1)
        span_end_probs = torch.nn.functional.softmax(span_end_logits, dim=-1)

        best_span = get_best_span(span_start_logits, span_end_logits)

        output_dict = {
            "span_start_logits": span_start_logits,
            "span_start_probs": span_start_probs,
            "span_end_logits": span_end_logits,
            "span_end_probs": span_end_probs,
            "best_span": best_span,
        }

        # Compute the loss for training.
        if span_start is not None:
            loss = nll_loss(util.masked_log_softmax(span_start_logits, passage_mask), span_start.squeeze(-1))
            self._span_start_accuracy(span_start_logits, span_start.squeeze(-1))
            loss += nll_loss(util.masked_log_softmax(span_end_logits, passage_mask), span_end.squeeze(-1))
            self._span_end_accuracy(span_end_logits, span_end.squeeze(-1))
            self._span_accuracy(best_span, torch.stack([span_start, span_end], -1))
            output_dict["loss"] = loss

        # Compute the EM and F1 on SQuAD and add the tokenized input to the output.
            # Compute the EM and F1 on SQuAD and add the tokenized input to the output.
            if metadata is not None:
                output_dict['best_span_str'] = []
                question_tokens = []
                passage_tokens = []
                metrics_per_item = None

                all_reference_answers_text = []
                all_best_spans = []

                return_metrics_per_item = True

                if not self.training:
                    metrics_per_item = [{} for x in range(batch_size)]

                for i in range(batch_size):
                    question_tokens.append(metadata[i]['question_tokens'])
                    passage_tokens.append(metadata[i]['passage_tokens'])
                    passage_str = metadata[i]['original_passage']
                    predicted_span = tuple(best_span[i].detach().cpu().numpy())

                    start_span = predicted_span[0]
                    end_span = predicted_span[1]
                    best_span_tokens = metadata[i]['passage_tokens'][start_span:end_span + 1]
                    best_span_string = " ".join(best_span_tokens)
                    output_dict['best_span_str'].append(best_span_string)
                    output_dict['best_span_tokens'] = best_span_tokens
                    answer_texts = metadata[i].get('answer_texts', [])

                    if return_output_metadata:
                        best_span_semantic_features = []
                        curr_item_features = passage_sem_views_q[i]
                        for view_id in range(curr_item_features.shape[0]):
                            curr_view_feats = curr_item_features[view_id][start_span:end_span + 1]
                            best_span_semantic_features.append(curr_view_feats.tolist())

                        output_dict['best_span_semantic_features'] = best_span_semantic_features

                    all_best_spans.append(best_span_string)

                    if answer_texts:
                        curr_item_em, curr_item_f1 = self._squad_metrics(best_span_string, answer_texts,
                                                                         return_score=True)
                        if not self.training and return_metrics_per_item:
                            metrics_per_item[i]["em"] = curr_item_em
                            metrics_per_item[i]["f1"] = curr_item_f1

                        all_reference_answers_text.append(answer_texts)

                # output metadata
                if return_output_metadata:
                    output_dict["output_metadata"] = {
                        "modeling_layer": {
                            "modeling_layer_iter_000": {
                                "encoder_block_001": {
                                    "semantic_views_q": passage_sem_views_q,
                                    "semantic_views_sent_mask": passage_sem_views_k,
                                },
                            }
                        }
                    }

                if not self.training and len(all_reference_answers_text) > 0:
                    metrics_per_item_rouge = self.calculate_rouge(all_best_spans, all_reference_answers_text,
                                                                  return_metrics_per_item=return_metrics_per_item)

                    for i, curr_metrics in enumerate(metrics_per_item_rouge):
                        metrics_per_item[i].update(curr_metrics)

                if metrics_per_item is not None:
                    output_dict['metrics'] = metrics_per_item

                output_dict['question_tokens'] = question_tokens
                output_dict['passage_tokens'] = passage_tokens
        return output_dict


    def calculate_rouge(self, predictions, references, return_metrics_per_item=False):
        # calculate rouge
        references_text = references
        predictions_text = predictions

        metrics_with_per_item_scores = self._rouge_evaluator.get_scores(predictions_text, references_text)

        metrics_per_item = []
        if return_metrics_per_item:
            metrics_per_item = [{} for x in range(len(predictions_text))]

        for metric, results in sorted(metrics_with_per_item_scores.items(), key=lambda x: x[0]):
            for hypothesis_id, results_per_ref in enumerate(results):
                # we report the max f-score of the two answers
                curr_item_rouge_f = max(results_per_ref['f'])
                self._rouge_scores[metric](curr_item_rouge_f)

                if return_metrics_per_item:
                    metrics_per_item[hypothesis_id][metric] = curr_item_rouge_f

        if return_metrics_per_item:
            return metrics_per_item


    def get_metrics(self, reset: bool = False) -> Dict[str, float]:
        exact_match, f1_score = self._squad_metrics.get_metric(reset)

        metrics = {
                'start_acc': self._span_start_accuracy.get_metric(reset),
                'end_acc': self._span_end_accuracy.get_metric(reset),
                'span_acc': self._span_accuracy.get_metric(reset),
                'em': exact_match,
                'f1': f1_score,
                }

        # # report bleu scores
        # for k,v in self._bleu_scores.items():
        #     metrics[k] = v.get_metric(reset)

        for k,v in self._rouge_scores.items():
            metrics[k] = v.get_metric(reset)

        return metrics
