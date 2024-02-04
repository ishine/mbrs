from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import torch

from mbrs.metrics import Metric, MetricCacheable

from . import register
from .mbr import DecoderMBR


@register("pruning_mbr")
class DecoderPruningMBR(DecoderMBR):
    """Pruning MBR decoder class.

    References:
        J. Cheng and A. Vlachos, 2023,
        "Faster Minimum Bayes Risk Decoding with Confidence-based Pruning".
        https://aclanthology.org/2023.emnlp-main.767/
    """

    cfg: Config

    def __init__(self, cfg: DecoderPruningMBR.Config, metric: Metric) -> None:
        super().__init__(cfg, metric)

    @dataclass
    class Config(DecoderMBR.Config):
        """Configuration for the decoder.

        - alpha (float): Prune hypotheses based on this confidence threshold.
        - sampling_shceduler (list[int]): Sample size scheduler. For each step, the
          number of samples will be the t-th number.
        - num_boostrap_samples (int): Number of boostrap samples.
        - seed (int): Random seed for bootstrap sampling. The random numbers are
          generated using PCG-64.
        """

        alpha: float = 0.99
        sampling_scheduler: list[int] = field(
            default_factory=lambda: [8, 16, 32, 64, 128, 256]
        )
        num_bootstrap_samples: int = 500
        seed: int = 0

    def decode_pruning(
        self,
        hypotheses: list[str],
        references: list[str],
        source: Optional[str] = None,
        nbest: int = 1,
    ) -> tuple[list[float], list[int]]:
        """Select the n-best hypotheses using pruning MBR decoding.

        Args:
            hypotheses (list[str]): Hypotheses.
            references (list[str]): References.
            source (str, optional): A source.
            nbest (int): Return the n-best hypotheses.

        Returns:
            - list[float]: Top-k scores.
            - list[int]: Top-k indices.
        """
        rng = torch.Generator(device=self.metric.device).manual_seed(self.cfg.seed)
        H = len(hypotheses)
        max_r = min(max(self.cfg.sampling_scheduler), len(references))
        pairwise_scores = torch.zeros((H, max_r), device=self.metric.device)
        orig_indices = torch.arange(H, device=self.metric.device)

        if isinstance(self.metric, MetricCacheable):
            source_ir = self.metric.encode([source]) if source is not None else None
            hypotheses_ir = self.metric.encode(hypotheses)

        # Algorithm 1 in the paper.
        prev_r = 0
        for t, r in enumerate(self.cfg.sampling_scheduler):
            r = min(r, len(references))
            if r <= prev_r:
                break

            # Equation 5 and Algorithm 2 in the paper.
            if isinstance(self.metric, MetricCacheable):
                references_ir = self.metric.encode(references[prev_r:r])
                pairwise_scores[:, prev_r:r] = self.metric.pairwise_scores_from_ir(
                    hypotheses_ir, references_ir, source_ir
                )
            else:
                pairwise_scores[:, prev_r:r] = self.metric.pairwise_scores(
                    hypotheses, references[prev_r:r], source
                )
            expected_scores = pairwise_scores.mean(dim=-1)
            current_best_idx = self.metric.argbest(expected_scores)
            sample_indices = torch.randint(
                r,
                size=(self.cfg.num_bootstrap_samples, r),
                device=self.metric.device,
                generator=rng,
            )
            bootstrap_expected_scores = pairwise_scores[:, sample_indices].mean(dim=-1)

            win_rates = (
                (
                    bootstrap_expected_scores
                    >= bootstrap_expected_scores[current_best_idx]
                )
                .float()
                .mean(dim=1)
            )
            winners = (win_rates > 1 - self.cfg.alpha).nonzero(as_tuple=True)[0]
            num_winners = len(winners)
            if num_winners >= nbest:
                if isinstance(self.metric, MetricCacheable):
                    hypotheses_ir = hypotheses_ir.index_select(0, winners)
                else:
                    hypotheses = [hypotheses[i] for i in winners]
                pairwise_scores = pairwise_scores[winners]
                orig_indices = orig_indices[winners]
                prev_r = r
            else:
                break
        expected_scores = pairwise_scores[:, :prev_r].mean(dim=1)
        topk_scores, topk_indices = self.metric.topk(expected_scores, k=nbest)
        return topk_scores, orig_indices[topk_indices].tolist()

    def decode(
        self,
        hypotheses: list[str],
        references: list[str],
        source: Optional[str] = None,
        nbest: int = 1,
    ) -> DecoderMBR.Output:
        """Select the n-best hypotheses based on the strategy.

        Args:
            hypotheses (list[str]): Hypotheses.
            references (list[str]): References.
            source (str, optional): A source.
            nbest (int): Return the n-best hypotheses.

        Returns:
            DecoderMBR.Output: The n-best hypotheses.
        """

        if len(hypotheses) <= nbest:
            expected_scores = self.metric.expected_scores(
                hypotheses, references, source
            )
            topk_scores, topk_indices = self.metric.topk(expected_scores, k=nbest)
        else:  # Pruning MBR decoding
            topk_scores, topk_indices = self.decode_pruning(
                hypotheses, references, source
            )

        return self.Output(
            idx=topk_indices,
            sentence=[hypotheses[idx] for idx in topk_indices],
            score=topk_scores,
        )