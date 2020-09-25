"""
Decoders and output normalization for CTC

Authors
 * Mirco Ravanelli 2020
 * Aku Rouhe 2020
"""
import torch
import numpy as np
from itertools import groupby


from speechbrain.data_io.data_io import length_to_mask


class CTCPrefixScoreTH(object):
    """Batch processing of CTCPrefixScore
    which is based on Algorithm 2 in WATANABE et al.
    "HYBRID CTC/ATTENTION ARCHITECTURE FOR END-TO-END SPEECH RECOGNITION,"
    but extended to efficiently compute the label probablities for multiple
    hypotheses simultaneously
    See also Seki et al. "Vectorized Beam Search for CTC-Attention-Based
    Speech Recognition," In INTERSPEECH (pp. 3825-3829), 2019.
    """

    def __init__(self, x, xlens, blank, eos, margin=0):
        """Construct CTC prefix scorer
        :param torch.Tensor x: input label posterior sequences (B, T, O)
        :param torch.Tensor xlens: input lengths (B,)
        :param int blank: blank label id
        :param int eos: end-of-sequence id
        :param int margin: margin parameter for windowing (0 means no windowing)
        """
        # In the comment lines,
        # we assume T: input_length, B: batch size, W: beam width, O: output dim.
        self.logzero = -10000000000.0
        self.blank = blank
        self.eos = eos
        self.batch = x.size(0)
        self.input_length = x.size(1)
        self.odim = x.size(2)
        self.dtype = x.dtype
        self.device = (
            torch.device("cuda:%d" % x.get_device())
            if x.is_cuda
            else torch.device("cpu")
        )
        # Pad the rest of posteriors in the batch
        # TODO(takaaki-hori): need a better way without for-loops
        # for i, l in enumerate(xlens):
        #     if l < self.input_length:
        #         x[i, l:, :] = self.logzero
        #         x[i, l:, blank] = 0

        # Reshape input x
        xn = x.transpose(0, 1)  # (B, T, O) -> (T, B, O)
        xb = xn[:, :, self.blank].unsqueeze(2).expand(-1, -1, self.odim)
        self.x = torch.stack([xn, xb])  # (2, T, B, O)
        self.end_frames = torch.as_tensor(xlens) - 1

        # Setup CTC windowing
        self.margin = margin
        if margin > 0:
            self.frame_ids = torch.arange(
                self.input_length, dtype=self.dtype, device=self.device
            )
        # Base indices for index conversion
        self.idx_bh = None
        self.idx_b = torch.arange(self.batch, device=self.device)
        self.idx_bo = (self.idx_b * self.odim).unsqueeze(1)

    def __call__(self, y, state, scoring_ids=None, att_w=None):
        """Compute CTC prefix scores for next labels
        :param list y: prefix label sequences
        :param tuple state: previous CTC state
        :param torch.Tensor pre_scores: scores for pre-selection of hypotheses (BW, O)
        :param torch.Tensor att_w: attention weights to decide CTC window
        :return new_state, ctc_local_scores (BW, O)
        """
        output_length = len(y[0]) - 1  # ignore sos
        last_ids = [yi[-1] for yi in y]  # last output label ids
        n_bh = len(last_ids)  # batch * hyps
        n_hyps = n_bh // self.batch  # assuming each utterance has the same # of hyps
        self.scoring_num = scoring_ids.size(-1) if scoring_ids is not None else 0
        # prepare state info
        if state is None:
            r_prev = torch.full(
                (self.input_length, 2, self.batch, n_hyps),
                self.logzero,
                dtype=self.dtype,
                device=self.device,
            )
            r_prev[:, 1] = torch.cumsum(self.x[0, :, :, self.blank], 0).unsqueeze(2)
            r_prev = r_prev.view(-1, 2, n_bh)
            s_prev = 0.0
            f_min_prev = 0
            f_max_prev = 1
        else:
            r_prev, s_prev, f_min_prev, f_max_prev = state

        # select input dimensions for scoring
        if self.scoring_num > 0:
            scoring_idmap = torch.full(
                (n_bh, self.odim), -1, dtype=torch.long, device=self.device
            )
            snum = self.scoring_num
            if self.idx_bh is None or n_bh > len(self.idx_bh):
                self.idx_bh = torch.arange(n_bh, device=self.device).view(-1, 1)
            scoring_idmap[self.idx_bh[:n_bh], scoring_ids] = torch.arange(
                snum, device=self.device
            )
            scoring_idx = (
                scoring_ids + self.idx_bo.repeat(1, n_hyps).view(-1, 1)
            ).view(-1)
            x_ = torch.index_select(
                self.x.view(2, -1, self.batch * self.odim), 2, scoring_idx
            ).view(2, -1, n_bh, snum)
        else:
            scoring_ids = None
            scoring_idmap = None
            snum = self.odim
            x_ = self.x.unsqueeze(3).repeat(1, 1, 1, n_hyps, 1).view(2, -1, n_bh, snum)

        # new CTC forward probs are prepared as a (T x 2 x BW x S) tensor
        # that corresponds to r_t^n(h) and r_t^b(h) in a batch.
        r = torch.full(
            (self.input_length, 2, n_bh, snum),
            self.logzero,
            dtype=self.dtype,
            device=self.device,
        )
        if output_length == 0:
            r[0, 0] = x_[0, 0]

        r_sum = torch.logsumexp(r_prev, 1)
        log_phi = r_sum.unsqueeze(2).repeat(1, 1, snum)
        if scoring_ids is not None:
            for idx in range(n_bh):
                pos = scoring_idmap[idx, last_ids[idx]]
                if pos >= 0:
                    log_phi[:, idx, pos] = r_prev[:, 1, idx]
        else:
            for idx in range(n_bh):
                log_phi[:, idx, last_ids[idx]] = r_prev[:, 1, idx]

        # decide start and end frames based on attention weights
        if att_w is not None and self.margin > 0:
            f_arg = torch.matmul(att_w, self.frame_ids)
            f_min = max(int(f_arg.min().cpu()), f_min_prev)
            f_max = max(int(f_arg.max().cpu()), f_max_prev)
            start = min(f_max_prev, max(f_min - self.margin, output_length, 1))
            end = min(f_max + self.margin, self.input_length)
        else:
            f_min = f_max = 0
            start = max(output_length, 1)
            end = self.input_length

        # compute forward probabilities log(r_t^n(h)) and log(r_t^b(h))
        for t in range(start, end):
            rp = r[t - 1]
            rr = torch.stack([rp[0], log_phi[t - 1], rp[0], rp[1]]).view(
                2, 2, n_bh, snum
            )
            r[t] = torch.logsumexp(rr, 1) + x_[:, t]

        # compute log prefix probabilites log(psi)
        log_phi_x = torch.cat((log_phi[0].unsqueeze(0), log_phi[:-1]), dim=0) + x_[0]
        if scoring_ids is not None:
            log_psi = torch.full(
                (n_bh, self.odim), self.logzero, dtype=self.dtype, device=self.device
            )
            log_psi_ = torch.logsumexp(
                torch.cat((log_phi_x[start:end], r[start - 1, 0].unsqueeze(0)), dim=0),
                dim=0,
            )
            for si in range(n_bh):
                log_psi[si, scoring_ids[si]] = log_psi_[si]
        else:
            log_psi = torch.logsumexp(
                torch.cat((log_phi_x[start:end], r[start - 1, 0].unsqueeze(0)), dim=0),
                dim=0,
            )

        for si in range(n_bh):
            log_psi[si, self.eos] = r_sum[self.end_frames[si // n_hyps], si]

        # exclude blank probs
        log_psi[:, self.blank] = self.logzero

        return (log_psi - s_prev), (r, log_psi, f_min, f_max, scoring_idmap)

    def index_select_state(self, state, best_ids):
        """Select CTC states according to best ids
        :param state    : CTC state
        :param best_ids : index numbers selected by beam pruning (B, W)
        :return selected_state
        """
        r, s, f_min, f_max, scoring_idmap = state
        # convert ids to BHO space
        n_bh = len(s)
        n_hyps = n_bh // self.batch
        vidx = (best_ids + (self.idx_b * (n_hyps * self.odim)).view(-1, 1)).view(-1)
        # vidx = best_ids
        # select hypothesis scores
        s_new = torch.index_select(s.view(-1), 0, vidx)
        # print(s.shape, best_ids.shape, n_hyps)
        # print(vidx.shape)
        # print(s_new.view(-1, 1).repeat(1, self.odim).shape)
        s_new = s_new.view(-1, 1).repeat(1, self.odim).view(n_bh, self.odim)
        # convert ids to BHS space (S: scoring_num)
        if scoring_idmap is not None:
            snum = self.scoring_num
            hyp_idx = (best_ids // self.odim + (self.idx_b * n_hyps).view(-1, 1)).view(
                -1
            )
            label_ids = torch.fmod(best_ids, self.odim).view(-1)
            score_idx = scoring_idmap[hyp_idx, label_ids]
            score_idx[score_idx == -1] = 0
            vidx = score_idx + hyp_idx * snum
        else:
            snum = self.odim
        # select forward probabilities
        r_new = torch.index_select(r.view(-1, 2, n_bh * snum), 2, vidx).view(
            -1, 2, n_bh
        )
        return r_new, s_new, f_min, f_max



class CTCPrefixScorer:
    def __init__(
        self, x, enc_lens, batch_size, beam_size, blank_index, eos_index
    ):
        self.blank_index = blank_index
        self.eos_index = eos_index
        self.max_enc_len = x.size(1)
        self.batch_size = batch_size
        self.beam_size = beam_size
        self.vocab_size = x.size(-1)
        self.device = x.device
        self.last_frame_index = enc_lens - 1

        # mask frames > enc_lens
        mask = 1 - length_to_mask(enc_lens)
        mask = mask.unsqueeze(-1).expand(-1, -1, x.size(-1)) == 1
        x.masked_fill_(mask, -np.inf)
        x[:, :, 0] = x[:, :, 0].masked_fill_(mask[:, :, 0], 0)

        # xnb: dim=0, nonblank posteriors, xb: dim=1, blank posteriors
        xnb = x.transpose(0, 1)
        xb = (
            xnb[:, :, self.blank_index]
            .unsqueeze(2)
            .expand(-1, -1, self.vocab_size)
        )

        # (2, L, batch_size * beam_size, vocab_size)
        self.x = torch.stack([xnb, xb])

        # The first index of each sentence.
        # TODO: for candidates mode
        self.beam_offset = (torch.arange(batch_size) * self.beam_size).to(
            self.device
        )

    def forward_step(self, g, state, candidates=None):
        """h = g + c"""
        prefix_length = g.size(1)
        last_char = [gi[-1] for gi in g] if prefix_length > 0 else [0] * len(g)
        num_candidates = (
            self.vocab_size
        )  # TODO support scoring for candidates, candidates.size(-1)

        if state is None:
            # r_prev: (max_enc_len, 2, batch_size * beam_size)
            r_prev = torch.Tensor(
                self.max_enc_len, 2, self.batch_size, self.beam_size
            ).to(self.device)
            r_prev.fill_(-np.inf)
            # Accumulate blank posteriors at each step
            r_prev[:, 1] = torch.cumsum(
                self.x[0, :, :, self.blank_index], 0
            ).unsqueeze(2)
            r_prev = r_prev.view(-1, 2, self.batch_size * self.beam_size)
            psi_prev = 0.0
        else:
            r_prev, psi_prev = state

        x_inflate = (
            self.x.unsqueeze(3)
            .repeat(1, 1, 1, self.beam_size, 1)
            .view(2, -1, self.batch_size * self.beam_size, self.vocab_size)
        )

        r = torch.Tensor(
            self.max_enc_len,
            2,
            self.batch_size * self.beam_size,
            num_candidates,
        ).to(self.device)
        r.fill_(-np.inf)

        if prefix_length == 0:
            r[0, 0] = x_inflate[0, 0]

        # TODO: scores for candidates

        # 0. phi = prev_nonblank + prev_blank = r_t-1^nb(g) + r_t-1^b(g), phi only depends on prefix g.
        r_sum = torch.logsumexp(r_prev, 1)
        phi = r_sum.unsqueeze(2).repeat(1, 1, num_candidates)

        # if last token of prefix g in candidates, phi = prev_b + 0
        for i in range(self.batch_size * self.beam_size):
            phi[:, i, last_char[i]] = r_prev[:, 1, i]

        # Define start, end, |g| < |h| for ctc decoding.
        start = max(1, prefix_length)
        end = self.max_enc_len

        # Compute forward prob log(r_t^nb(h)) and log(r_t^b(h))
        for t in range(start, end):
            # 1. p(h|cur step is nonblank) = [p(prev step=y) + phi] * p(c)
            r[t, 0] = torch.logsumexp(
                torch.stack((r[t - 1, 0], phi[t - 1]), dim=0), dim=0
            )
            r[t, 0] = r[t, 0] + x_inflate[0, t]
            # 2. p(h|cur step is blank) = [p(prev step is blank) + p(prev step is nonblank)] * p(blank)
            r[t, 1] = torch.logsumexp(
                torch.stack((r[t - 1, 0], r[t - 1, 1]), dim=0), dim=0
            )
            r[t, 1] = r[t, 1] + x_inflate[1, t]

        # Compute the predix prob
        psi = r[start - 1, 0].unsqueeze(0)
        # phi is prob at t-1 step, shift one frame then add it to current prob p(c)
        phix = torch.cat((phi[0].unsqueeze(0), phi[:-1]), dim=0) + x_inflate[0]
        # 3. psi = psi + phi * p(c)
        psi = torch.logsumexp(torch.cat((phix[start:end], psi), dim=0), dim=0)

        # if c = <eos>, log(r_T^n(g) + r_T^b(g)), where T is the max frames of enc_states
        for i in range(self.batch_size * self.beam_size):
            psi[i, self.eos_index] = r_sum[
                self.last_frame_index[i // self.beam_size], i
            ]

        # exclude blank probs for joint scoring
        # TODO: currently comment out this line since bos_index, eos_indx is the same as blank_index
        psi[:, self.blank_index] = -np.inf

        return psi - psi_prev, (r, psi)

    def permute_mem(self, memory, candidates):
        r, psi = memory
        best_index = (
            candidates
            + (
                self.beam_offset.unsqueeze(1).expand_as(candidates)
                * self.vocab_size
            )
        ).view(-1)
        r = torch.index_select(
            r.view(-1, 2, self.batch_size * self.beam_size * self.vocab_size),
            dim=-1,
            index=best_index,
        )
        r = r.view(-1, 2, self.batch_size * self.beam_size)

        psi = torch.index_select(psi.view(-1), dim=0, index=best_index)
        psi = (
            psi.view(-1, 1)
            .repeat(1, self.vocab_size)
            .view(self.batch_size * self.beam_size, self.vocab_size)
        )

        return r, psi


def filter_ctc_output(string_pred, blank_id=-1):
    """Apply CTC output merge and filter rules.

    Removes the blank symbol and output repetitions.

    Parameters
    ----------
    string_pred : list
        a list containing the output strings/ints predicted by the CTC system
    blank_id : int, string
        the id of the blank

    Returns
    ------
    list
        The output predicted by CTC without the blank symbol and
        the repetitions

    Example
    -------
        >>> string_pred = ['a','a','blank','b','b','blank','c']
        >>> string_out = filter_ctc_output(string_pred, blank_id='blank')
        >>> print(string_out)
        ['a', 'b', 'c']
    """

    if isinstance(string_pred, list):
        # Filter the repetitions
        string_out = [
            v
            for i, v in enumerate(string_pred)
            if i == 0 or v != string_pred[i - 1]
        ]

        # Remove duplicates
        string_out = [i[0] for i in groupby(string_out)]

        # Filter the blank symbol
        string_out = list(filter(lambda elem: elem != blank_id, string_out))
    else:
        raise ValueError("filter_ctc_out can only filter python lists")
    return string_out


def ctc_greedy_decode(probabilities, seq_lens, blank_id=-1):
    """
    Greedy decode a batch of probabilities and apply CTC rules

    Parameters
    ----------
    probabilities : torch.tensor
        Output probabilities (or log-probabilities) from network with shape
        [batch, probabilities, time]
    seq_lens : torch.tensor
        Relative true sequence lengths (to deal with padded inputs),
        longest sequence has length 1.0, others a value betwee zero and one
        shape [batch, lengths]
    blank_id : int, string
        The blank symbol/index. Default: -1. If a negative number is given,
        it is assumed to mean counting down from the maximum possible index,
        so that -1 refers to the maximum possible index.

    Returns
    -------
    list
        Outputs as Python list of lists, with "ragged" dimensions; padding
        has been removed.

    Example
    -------
        >>> import torch
        >>> probs = torch.tensor([[[0.3, 0.7], [0.0, 0.0]],
        ...                       [[0.2, 0.8], [0.9, 0.1]]])
        >>> lens = torch.tensor([0.51, 1.0])
        >>> blank_id = 0
        >>> ctc_greedy_decode(probs, lens, blank_id)
        [[1], [1]]
    """
    if isinstance(blank_id, int) and blank_id < 0:
        blank_id = probabilities.shape[-1] + blank_id
    batch_max_len = probabilities.shape[1]
    batch_outputs = []
    for seq, seq_len in zip(probabilities, seq_lens):
        actual_size = int(torch.round(seq_len * batch_max_len))
        scores, predictions = torch.max(seq.narrow(0, 0, actual_size), dim=1)
        out = filter_ctc_output(predictions.tolist(), blank_id=blank_id)
        batch_outputs.append(out)
    return batch_outputs
