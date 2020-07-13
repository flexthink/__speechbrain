"""Transformer implementaion in the SpeechBrain sytle

Authors
* Jianyuan Zhong 2020
"""

import torch
import math
import torch.nn as nn
from speechbrain.nnet.attention import (
    MultiheadAttention,
    PositionalwiseFeedForward,
)
from speechbrain.nnet.normalization import LayerNorm


class TransformerInterface(nn.Module):
    """This is an interface for transformer model. Users can modify the attributes and
    define the forward function as needed according to their own tasks.

    The architecture is based on the paper "Attention Is All You Need": https://arxiv.org/pdf/1706.03762.pdf

    Arguements
    ----------
    d_model: int
        the number of expected features in the encoder/decoder inputs (default=512).
    nhead: int
        the number of heads in the multiheadattention models (default=8).
    num_encoder_layers: int
        the number of sub-encoder-layers in the encoder (default=6).
    num_decoder_layers: int
        the number of sub-decoder-layers in the decoder (default=6).
    dim_ffn: int
        the dimension of the feedforward network model (default=2048).
    dropout: int
        the dropout value (default=0.1).
    activation: torch class
        the activation function of encoder/decoder intermediate layer, relu or gelu (default=relu)
    custom_src_module: torch class
        module that process the src features to expected feature dim
    custom_tgt_module: torch class
        module that process the src features to expected feature dim
    """

    def __init__(
        self,
        d_model=512,
        nhead=8,
        num_encoder_layers=6,
        num_decoder_layers=6,
        d_ffn=2048,
        dropout=0.1,
        activation=nn.ReLU,
        custom_src_module=None,
        custom_tgt_module=None,
        return_attention=False,
    ):
        super().__init__()

        assert (
            num_encoder_layers + num_decoder_layers > 0
        ), "number of encoder layers and number of decoder layers cannot both be 0!"

        # initialize the encoder
        if num_encoder_layers > 0:
            if custom_src_module is not None:
                self.custom_src_module = custom_src_module(d_model)

            self.encoder = TransformerEncoder(
                nhead=nhead,
                num_layers=num_encoder_layers,
                d_ffn=d_ffn,
                dropout=dropout,
                activation=activation,
                return_attention=return_attention,
            )

        # initialize the dncoder
        if num_encoder_layers > 0:
            if custom_tgt_module is not None:
                self.custom_tgt_module = custom_tgt_module(d_model)

            self.decoder = TransformerDecoder(
                num_layers=num_decoder_layers,
                nhead=nhead,
                d_ffn=d_ffn,
                dropout=dropout,
                activation=activation,
                return_attention=return_attention,
            )

    def forward(self, **kwags):
        """Users should modify this function according to their own tasks
        """
        raise NotImplementedError


class PositionalEncoding(nn.Module):
    """This class implements the positional encoding function

    PE(pos, 2i)   = sin(pos/(10000^(2i/dmodel)))
    PE(pos, 2i+1) = cos(pos/(10000^(2i/dmodel)))

    Arguements
    ----------
    max_len :
        max length of the input sequences (default 2500)

    Example
    -------
    >>> a = torch.rand((8, 120, 512))
    >>> enc = PositionalEncoding()
    >>> b = enc(a, init_params=True)
    >>> print(b.shape)
    torch.Size([1, 120, 512])
    """

    def __init__(self, dropout=0.1, max_len=2500):
        super().__init__()
        self.max_len = max_len
        self.dropout = dropout

    def init_params(self, first_input):
        model_dim = first_input.shape[-1]
        pe = torch.zeros(self.max_len, model_dim, requires_grad=False)
        positions = torch.arange(0, self.max_len).unsqueeze(1).float()
        denominator = torch.exp(
            torch.arange(0, model_dim, 2).float()
            * -(math.log(10000.0) / model_dim)
        )

        pe[:, 0::2] = torch.sin(positions * denominator)
        pe[:, 1::2] = torch.cos(positions * denominator)
        pe = pe.unsqueeze(0).to(first_input.device)
        self.register_buffer("pe", pe)

    def forward(self, x, init_params=False):
        """
        Arguements
        ----------
        x:
            input feature (batch, time, fea)
        """
        if init_params:
            self.init_params(x)
        return self.pe[:, : x.size(1)].clone().detach()


class TransformerEncoderLayer(nn.Module):
    """ This is an implementation of self-attention encoder layer

    Arguements
    ----------
    d_ffn :
        Hidden size of self-attention Feed Forward layer
    nhead :
        number of attention heads
    kdim :
        dimension for key
    vdim :
        dimension for value
    dropout :
        dropout for the encoder

    Example
    -------
    >>> import torch
    >>> x = torch.rand((8, 60, 512))
    >>> net = TransformerEncoderLayer(512, 8)
    >>> output = net(x, init_params=True)
    >>> print(output[0].shape)
    torch.Size([8, 60, 512])
    """

    def __init__(
        self,
        d_ffn,
        nhead,
        kdim=None,
        vdim=None,
        dropout=0.1,
        activation=nn.ReLU,
    ):
        super().__init__()
        self.self_att = MultiheadAttention(
            nhead=nhead, dropout=dropout, kdim=kdim, vdim=vdim
        )
        self.pos_ffn = PositionalwiseFeedForward(
            d_ffn=d_ffn, dropout=dropout, activation=activation
        )
        # self.norm = LayerNorm(eps=1e-6)

    def forward(
        self, src, src_mask=None, src_key_padding_mask=None, init_params=False
    ):
        output, self_attn = self.self_att(
            src,
            src,
            src,
            attn_mask=src_mask,
            key_padding_mask=src_key_padding_mask,
            init_params=init_params,
        )
        # output = self.norm(src + output, init_params)
        output = self.pos_ffn(output, init_params)

        return output, self_attn


class TransformerEncoder(nn.Module):
    """This class implements the transformer encoder

    Arguements
    ----------
    d_ffn :
        Hidden size of self-attention Feed Forward layer
    nhead :
        number of attention heads
    kdim :
        dimension for key
    vdim :
        dimension for value
    dropout :
        dropout for the encoder
    input_module: torch class
        the module to process the source input feature to expected feature dimension

    Example
    -------
    >>> import torch
    >>> x = torch.rand((8, 60, 512))
    >>> net = TransformerEncoder(1, 8, 512, 512)
    >>> output = net(x, init_params=True)
    >>> print(output.shape)
    torch.Size([8, 60, 512])
    """

    def __init__(
        self,
        num_layers,
        nhead,
        d_ffn,
        kdim=None,
        vdim=None,
        dropout=0.1,
        activation=nn.ReLU,
        return_attention=False,
    ):
        super().__init__()
        self.layers = torch.nn.ModuleList(
            [
                TransformerEncoderLayer(
                    d_ffn=d_ffn,
                    nhead=nhead,
                    kdim=kdim,
                    vdim=vdim,
                    dropout=dropout,
                    activation=activation,
                )
                for _ in range(num_layers)
            ]
        )
        self.norm = LayerNorm(eps=1e-6)
        self.drop = nn.Dropout(dropout)
        self.return_attention = return_attention

    def forward(
        self, src, src_mask=None, src_key_padding_mask=None, init_params=False
    ):
        output = src
        attention_lst = []
        for enc_layer in self.layers:
            output, attention = enc_layer(
                output,
                src_mask=src_mask,
                src_key_padding_mask=src_key_padding_mask,
                init_params=init_params,
            )
            attention_lst.append(attention)
        output = self.norm(output, init_params)

        if self.return_attention:
            return output, attention_lst
        return output


class TransformerDecoderLayer(nn.Module):
    """This class implements the self-attention decoder layer
    """

    def __init__(
        self,
        d_ffn,
        nhead,
        kdim=None,
        vdim=None,
        dropout=0.1,
        activation=nn.ReLU,
    ):
        super().__init__()
        self.self_attn = MultiheadAttention(
            nhead=nhead, kdim=kdim, vdim=vdim, dropout=dropout
        )
        self.mutihead_attn = MultiheadAttention(
            nhead=nhead, kdim=kdim, vdim=vdim, dropout=dropout
        )
        self.pos_ffn = PositionalwiseFeedForward(
            d_ffn=d_ffn, dropout=dropout, activation=activation
        )

        # normalization layers
        # self.norm1 = LayerNorm(eps=1e-6)
        # self.norm2 = LayerNorm(eps=1e-6)

    def forward(
        self,
        tgt,
        memory,
        tgt_mask=None,
        memory_mask=None,
        tgt_key_padding_mask=None,
        memory_key_padding_mask=None,
        init_params=False,
    ):
        tgt2, self_attn = self.self_attn(
            tgt,
            tgt,
            tgt,
            attn_mask=tgt_mask,
            key_padding_mask=tgt_key_padding_mask,
            init_params=init_params,
        )
        # tgt = self.norm1(tgt + tgt2, init_params)

        tgt2, multihead_attention = self.mutihead_attn(
            tgt,
            memory,
            memory,
            attn_mask=memory_mask,
            key_padding_mask=memory_key_padding_mask,
            init_params=init_params,
        )
        # tgt = self.norm2(tgt + tgt2, init_params)

        tgt = self.pos_ffn(tgt, init_params)
        return tgt, self_attn, multihead_attention


class TransformerDecoder(nn.Module):
    """This class implements the Transformer decoder

    Arguements
    ----------
    d_model:
        the number of expected features in the encoder inputs
    d_ffn :
        Hidden size of self-attention Feed Forward layer
    nhead :
        number of attention heads
    kdim :
        dimension for key
    vdim :
        dimension for value
    dropout :
        dropout for the decoder
    """

    def __init__(
        self,
        num_layers,
        nhead,
        d_ffn,
        kdim=None,
        vdim=None,
        dropout=0.1,
        activation=nn.ReLU,
        return_attention=False,
    ):
        super().__init__()
        self.layers = torch.nn.ModuleList(
            [
                TransformerDecoderLayer(
                    d_ffn=d_ffn,
                    nhead=nhead,
                    kdim=kdim,
                    vdim=vdim,
                    dropout=dropout,
                    activation=activation,
                )
                for _ in range(num_layers)
            ]
        )
        self.norm = LayerNorm(eps=1e-6)
        # self.drop = nn.Dropout(dropout)
        self.return_attention = return_attention

    def forward(
        self,
        tgt,
        memory,
        tgt_mask=None,
        memory_mask=None,
        tgt_key_padding_mask=None,
        memory_key_padding_mask=None,
        init_params=False,
    ):
        output = tgt
        self_attns, multihead_attns = [], []
        for dec_layer in self.layers:
            output, self_attn, multihead_attn = dec_layer(
                output,
                memory,
                tgt_mask=tgt_mask,
                memory_mask=memory_mask,
                tgt_key_padding_mask=tgt_key_padding_mask,
                memory_key_padding_mask=memory_key_padding_mask,
                init_params=init_params,
            )
            self_attns.append(self_attn)
            multihead_attns.append(multihead_attn)
        output = self.norm(output, init_params)

        if self.return_attention:
            return output, self_attns, multihead_attns
        return output


def get_key_padding_mask(padded_input, pad_idx):
    """Create a binary mask to prevent attention to padded locations

    Arguements
    ----------
    padded_input: int
        padded input
    pad_idx:
        idx for padding element

    Example
    -------
    >>> a = torch.LongTensor([[1,1,0], [2,3,0], [4,5,0]])
    >>> km = get_key_padding_mask(a, pad_idx=0)
    >>> print(km)
    tensor([[False, False,  True],
            [False, False,  True],
            [False, False,  True]])
    """
    if len(padded_input.shape) == 4:
        bz, time, ch1, ch2 = padded_input.shape
        padded_input = padded_input.reshape(bz, time, ch1 * ch2)

    key_padded_mask = padded_input.eq(pad_idx)

    # if the input is more than 2d, mask the locations where they are silence across all channels
    if len(padded_input.shape) > 2:
        key_padded_mask = key_padded_mask.float().prod(dim=-1).bool()
        return key_padded_mask.detach()

    return key_padded_mask.detach()


def get_lookahead_mask(padded_input):
    """Creates a binary mask for each sequence.

    Arguements
    ----------
    padded_input : tensor

    Example
    -------
    >>> a = torch.LongTensor([[1,1,0], [2,3,0], [4,5,0]])
    >>> sm = get_lookahead_mask(a)
    >>> print(sm)
    tensor([[0., -inf, -inf],
            [0., 0., -inf],
            [0., 0., 0.]])
    """
    batch_size, seq_len = padded_input.shape
    mask = (torch.triu(torch.ones(seq_len, seq_len)) == 1).transpose(0, 1)
    mask = (
        mask.float()
        .masked_fill(mask == 0, float("-inf"))
        .masked_fill(mask == 1, float(0.0))
    )
    return mask.detach().to(padded_input.device)