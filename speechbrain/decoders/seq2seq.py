"""
Decoding methods for seq2seq autoregressive model.

Author:
    Ju-Chieh Chou 2020
"""
import torch
import numpy as np


class BaseSearcher(torch.nn.Module):
    """
    BaseSearcher class to be inherited by other
    decoding approches for autoregressive model.

    Parameters
    ----------
    modules : No limit
        The modules user uses to perform search algorithm.
    bos_index : int
        The index of beginning-of-sequence token.
    eos_index : int
        The index of end-of-sequence token.
    min_decode_radio : float
        The ratio of minimum decoding steps to length of encoder states.
    max_decode_radio : float
        The ratio of maximum decoding steps to length of encoder states.

    Returns
    -------
    predictions:
        Outputs as Python list of lists, with "ragged" dimensions; padding
        has been removed.
    scores:
        The sum of log probabilities (and possibly
        additional heuristic scores) for each prediction.

    """

    def __init__(
        self, modules, bos_index, eos_index, min_decode_ratio, max_decode_ratio
    ):
        super(BaseSearcher, self).__init__()
        self.modules = modules
        self.bos_index = bos_index
        self.eos_index = eos_index
        self.min_decode_ratio = min_decode_ratio
        self.max_decode_ratio = max_decode_ratio

    def forward(self, enc_states, wav_len):
        """This method should implement the forward algorithm of decoding method.

        Arguments
        ---------
        enc_states : torch.Tensor
            The hidden states sequences to be attended.
        wav_len : torch.Tensor
            The speechbrain-style relative length.

        """
        raise NotImplementedError

    def forward_step(self, inp_tokens, memory, enc_states, enc_lens):
        """This method should implement one step of
        forwarding operation in autoregressive model.

        Arguments
        ---------
        inp_tokens : torch.Tensor
            The input tensor of current timestep.
        memory : No limit
            The momory variables input for this timestep.
            (ex. RNN hidden states).
        enc_states : torch.Tensor
            The encoder states to be attended.
        enc_lens : torch.Tensor
            The actual length of each enc_states sequence.

        Return
        ------
        log_probs : torch.Tensor
            Log-probilities of the current timestep output.
        memory : No limit
            The momory variables generated in this timestep.
            (ex. RNN hidden states).
        """
        raise NotImplementedError

    def reset_mem(self, batch_size, device):
        """This method should implement the reseting of
        memory variables in the decoding approaches.
        Ex. Initializing zero vector as initial hidden states.

        Arguments
        ---------
        batch_size : int
            The size of the batch.
        device : torch.device
            The device to put the initial variables.

        Return
        ------
        memory : No limit
            The initial memory variable.
        """
        raise NotImplementedError


class GreedySearcher(BaseSearcher):
    def forward(self, enc_states, wav_len):
        """
        This class implements the general forward-pass of
        greedy decoding approach.
        """
        enc_lens = torch.round(enc_states.shape[1] * wav_len).int()
        device = enc_states.device
        batch_size = enc_states.shape[0]

        memory = self.reset_mem(batch_size, device=device)
        inp_tokens = (
            enc_states.new_zeros(batch_size).fill_(self.bos_index).long()
        )

        log_probs_lst = []
        max_decode_steps = enc_states.shape[1] * self.max_decode_ratio

        for t in range(max_decode_steps):
            log_probs, memory = self.forward_step(
                inp_tokens, memory, enc_states, enc_lens
            )
            log_probs_lst.append(log_probs)
            inp_tokens = log_probs.argmax(dim=-1)

        log_probs = torch.stack(log_probs_lst, dim=1)
        scores, predictions = log_probs.max(dim=-1)
        scores = scores.sum(dim=1).tolist()
        predictions = batch_filter_seq2seq_output(
            predictions, eos_id=self.eos_index
        )

        return predictions, scores


class RNNGreedySearcher(GreedySearcher):
    """
    This class implements the greedy decoding
    for AttentionalRNNDecoder (speechbrain/nnet/RNN.py).

    Parameters
    ----------
    modules : list of torch.nn.Module
        The list should contain four items:
            1. Embedding layer
            2. Attentional RNN decoder
            3. Output layer
            4. Softmax layer
    """

    def __init__(
        self, modules, bos_index, eos_index, min_decode_ratio, max_decode_ratio,
    ):
        super(RNNGreedySearcher, self).__init__(
            modules, bos_index, eos_index, min_decode_ratio, max_decode_ratio
        )
        self.emb = modules[0]
        self.dec = modules[1]
        self.lin = modules[2]
        self.softmax = modules[3]

    def reset_mem(self, batch_size, device):
        hs = None
        self.dec.attn.reset()
        c = torch.zeros(batch_size, self.dec.attn_dim).to(device)
        return hs, c

    def forward_step(self, inp_tokens, memory, enc_states, enc_lens):
        hs, c = memory
        e = self.emb(inp_tokens)
        dec_out, hs, c, w = self.dec.forward_step(
            e, hs, c, enc_states, enc_lens
        )
        log_probs = self.softmax(self.lin(dec_out))
        return log_probs, (hs, c)


class BeamSearcher(BaseSearcher):
    """
    This class implements the beam-search algorithm for autoregressive model.

    Parameters
    ----------
    beam_size : int
        The width of beam.
    length_penalty : float
        The coefficient of length penalty (γ).
        log P(y|x) + λ log P_LM(y) + γ*len(y
    eos_threshold : float
        The threshold coefficient for eos token. See 3.1.2 in
        reference: https://arxiv.org/abs/1904.02619
    minus_inf : float
        The value of minus infinity to block some path
        of the search (default : -1e20).
    """

    def __init__(
        self,
        modules,
        bos_index,
        eos_index,
        min_decode_ratio,
        max_decode_ratio,
        beam_size,
        length_penalty,
        eos_threshold,
        minus_inf=-1e20,
    ):
        super(BeamSearcher, self).__init__(
            modules, bos_index, eos_index, min_decode_ratio, max_decode_ratio
        )
        self.beam_size = beam_size
        self.length_penalty = length_penalty
        self.eos_threshold = eos_threshold
        self.minus_inf = minus_inf

    def forward(self, enc_states, wav_len):
        enc_lens = torch.round(enc_states.shape[1] * wav_len).int()
        device = enc_states.device
        batch_size = enc_states.shape[0]

        enc_states = torch.repeat_interleave(enc_states, self.beam_size, dim=0)
        enc_lens = torch.repeat_interleave(enc_lens, self.beam_size, dim=0)

        memory = self.reset_mem(batch_size * self.beam_size, device=device)
        inp_tokens = (
            enc_states.new_zeros(batch_size * self.beam_size)
            .fill_(self.bos_index)
            .long()
        )

        # The first index of each sentence.
        beam_offset = (torch.arange(batch_size) * self.beam_size).to(device)

        # initialize sequence scores variables.
        sequence_scores = torch.Tensor(batch_size * self.beam_size).to(device)
        sequence_scores.fill_(-np.inf)

        # keep only the first to make sure no redundancy.
        sequence_scores.index_fill_(0, beam_offset, 0.0)

        # keep the hypothesis that reaches eos and their corresponding score.
        hyps_and_scores = [[] for _ in range(batch_size)]

        # keep the sequences that still not reaches eos.
        alived_seq = (
            torch.empty(batch_size * self.beam_size, 0).long().to(device)
        )

        min_decode_steps = enc_states.shape[1] * self.min_decode_ratio
        max_decode_steps = enc_states.shape[1] * self.max_decode_ratio

        for t in range(max_decode_steps):
            log_probs, memory = self.forward_step(
                inp_tokens, memory, enc_states, enc_lens
            )
            vocab_size = log_probs.shape[-1]

            # Set eos to minus_inf when less than minimum steps.
            if t < min_decode_steps:
                log_probs[:, self.eos_index] = self.minus_inf

            # Set the eos prob to minus_inf when it doesn't exceed threshold.
            max_probs, _ = torch.max(log_probs[:, : self.eos_index], dim=-1)
            eos_probs = log_probs[:, self.eos_index]
            log_probs[:, self.eos_index] = torch.where(
                eos_probs > self.eos_threshold * max_probs,
                eos_probs,
                torch.Tensor([self.minus_inf]).to(device),
            )

            scores = sequence_scores.unsqueeze(1).expand(-1, vocab_size)
            scores = scores + log_probs

            # keep topk beams
            scores, candidates = scores.view(batch_size, -1).topk(
                self.beam_size, dim=-1
            )

            # The input for the next step, also the output of current step.
            inp_tokens = (candidates % vocab_size).view(
                batch_size * self.beam_size
            )
            sequence_scores = scores.view(batch_size * self.beam_size)

            # The index where the current top-K output came from (t-1).
            predecessors = (
                candidates // vocab_size
                + beam_offset.unsqueeze(1).expand_as(candidates)
            ).view(batch_size * self.beam_size)

            # Permute the memory to synchoronize with the output.
            memory = self.permute_mem(memory, index=predecessors)

            # Update alived_seq
            alived_seq = torch.cat(
                [
                    torch.index_select(alived_seq, dim=0, index=predecessors),
                    inp_tokens.unsqueeze(1),
                ],
                dim=-1,
            )
            is_eos = inp_tokens.eq(self.eos_index)
            eos_indices = is_eos.nonzero()

            # Keep the hypothesis and their score when reaching eos.
            if eos_indices.shape[0] > 0:
                for index in eos_indices:
                    # convert to int
                    index = index.item()
                    batch_id = index // self.beam_size
                    if len(hyps_and_scores[batch_id]) == self.beam_size:
                        continue
                    hyp = alived_seq[index, :]
                    final_scores = sequence_scores[
                        index
                    ].item() + self.length_penalty * (t + 1)
                    hyps_and_scores[batch_id].append((hyp, final_scores))

                # Block the path that has reaches eos
                sequence_scores.masked_fill_(is_eos, -np.inf)

        # Check whether there are number of beam_size hypothesis.
        for i in range(batch_size):
            batch_offset = i * self.beam_size
            n_hyps = len(hyps_and_scores[i])

            # If not, add the top-scored ones.
            if n_hyps < self.beam_size:
                remains = self.beam_size - n_hyps
                hyps = alived_seq[batch_offset : batch_offset + remains, :]
                scores = (
                    sequence_scores[batch_offset : batch_offset + remains]
                    + self.length_penalty * max_decode_steps
                ).tolist()
                hyps_and_scores[i] += list(zip(hyps, scores))

        predictions, top_scores = [], []
        for i in range(batch_size):
            top_hyp, top_score = max(
                hyps_and_scores[i], key=lambda pair: pair[1]
            )
            predictions.append(top_hyp)
            top_scores.append(top_score)

        predictions = batch_filter_seq2seq_output(
            predictions, eos_id=self.eos_index
        )

        return predictions, top_scores

    def permute_mem(self, memory, index):
        """
        This method permutes the memory to synchorize
        the memory to current output.

        Arguments
        ---------

        memory : No limit
            The memory variable to be permuted.
        index : torch.Tensor
            The index of the previous path.

        Return
        ------
        The variable of the memory being permuted.

        """
        raise NotImplementedError


class RNNBeamSearcher(BeamSearcher):
    def __init__(
        self,
        modules,
        bos_index,
        eos_index,
        min_decode_ratio,
        max_decode_ratio,
        beam_size,
        length_penalty,
        eos_threshold,
    ):
        super(RNNBeamSearcher, self).__init__(
            modules,
            bos_index,
            eos_index,
            min_decode_ratio,
            max_decode_ratio,
            beam_size,
            length_penalty,
            eos_threshold,
        )
        self.emb = modules[0]
        self.dec = modules[1]
        self.lin = modules[2]
        self.softmax = modules[3]

    def reset_mem(self, batch_size, device):
        hs = None
        self.dec.attn.reset()
        c = torch.zeros(batch_size, self.dec.attn_dim).to(device)
        return hs, c

    def forward_step(self, inp_tokens, memory, enc_states, enc_lens):
        hs, c = memory
        e = self.emb(inp_tokens)
        dec_out, hs, c, w = self.dec.forward_step(
            e, hs, c, enc_states, enc_lens
        )
        log_probs = self.softmax(self.lin(dec_out))
        return log_probs, (hs, c)

    def permute_mem(self, memory, index):
        hs, c = memory

        # shape of hs: [num_layers, batch_size, n_neurons]
        if isinstance(hs, tuple):
            hs_0 = torch.index_select(hs[0], dim=1, index=index)
            hs_1 = torch.index_select(hs[1], dim=1, index=index)
            hs = (hs_0, hs_1)
        else:
            hs = torch.index_select(hs, dim=1, index=index)

        c = torch.index_select(c, dim=0, index=index)
        if self.dec.attn_type == "location":
            self.dec.attn.prev_attn = torch.index_select(
                self.dec.attn.prev_attn, dim=0, index=index
            )
        return (hs, c)


def batch_filter_seq2seq_output(prediction, eos_id=-1):
    """Calling batch_size times of filter_seq2seq_output.

    Parameters
    ----------
    prediction : list of torch.Tensor
        a list containing the output ints predicted by the seq2seq system.
    eos_id : int, string
        the id of the eos.

    Returns
    ------
    list
        The output predicted by seq2seq model.

    Example
    -------
        >>> predictions = [torch.IntTensor([1,2,3,4]), torch.IntTensor([2,3,4,5,6])]
        >>> predictions = batch_filter_seq2seq_output(predictions, eos_id=4)
        >>> predictions
        [[1, 2, 3], [2, 3]]
    """
    outputs = []
    for p in prediction:
        res = filter_seq2seq_output(p.tolist(), eos_id=eos_id)
        outputs.append(res)
    return outputs


def filter_seq2seq_output(string_pred, eos_id=-1):
    """Filter the output until the first eos occurs (exclusive).

    Parameters
    ----------
    string_pred : list
        a list containing the output strings/ints predicted by the seq2seq system.
    eos_id : int, string
        the id of the eos.

    Returns
    ------
    list
        The output predicted by seq2seq model.

    Example
    -------
        >>> string_pred = ['a','b','c','d','eos','e']
        >>> string_out = filter_seq2seq_output(string_pred, eos_id='eos')
        >>> string_out
        ['a', 'b', 'c', 'd']
    """
    if isinstance(string_pred, list):
        try:
            eos_index = next(
                i for i, v in enumerate(string_pred) if v == eos_id
            )
        except StopIteration:
            eos_index = len(string_pred)
        string_out = string_pred[:eos_index]
    else:
        raise ValueError("The input must be a list.")
    return string_out
