"""Library implementing complex-valued recurrent neural networks.

Authors
 * Titouan Parcollet 2020
"""

import torch
import logging
from speechbrain.nnet.complex_networks.complex_ops import complex_linear

logger = logging.getLogger(__name__)


class ComplexRNN(torch.nn.Module):
    """ This function implements a vanilla complex-valued RNN.

    Input format is (batch, time, fea) or (batch, time, fea, channel).
    In the latter shape, the two last dimensions will be merged:
    (batch, time, fea * channel)

    Arguments
    ---------
    hidden_size: int
        Number of output neurons (i.e, the dimensionality of the output).
        Specified value is in term of complex-valued neurons. Thus, the output
        is 2*hidden_size.
    num_layers: int, optional
        Default: 1
        Number of layers to employ in the RNN architecture.
    nonlinearity: str, optional
        Default: tanh
        Type of nonlinearity (tanh, relu).
    bias: bool, optional
        Default: True
        If True, the additive bias b is adopted.
    dropout: float, optional
        Default: 0.0
        It is the dropout factor (must be between 0 and 1).
    return_hidden: bool, optional
        Default: False
        It True, the function returns the last hidden layer.
    bidirectional: bool, optional
        Default: False
        If True, a bidirectioal model that scans the sequence both
        right-to-left and left-to-right is used.
    init_criterion: str , optional
        Default: he.
        (glorot, he).
        This parameter controls the initialization criterion of the weights.
        It is combined with weights_init to build the initialization method of
        the complex-valued weights.
    weight_init: str, optional
        Default: complex.
        (complex, unitary).
        This parameter defines the initialization procedure of the
        complex-valued weights. "complex" will generate random complex-valued
        weights following the init_criterion and the complex polar form.
        "unitary" will normalize the weights to lie on the unit circle.
        More details in: "Deep Complex Networks", Trabelsi C. et al.


    Example
    -------
    >>> inp_tensor = torch.rand([10, 16, 30])
    >>> rnn = ComplexRNN(hidden_size=16)
    >>> out_tensor = rnn(inp_tensor, init_params=True)
    >>>
    torch.Size([10, 16, 64])
    """

    def __init__(
        self,
        hidden_size,
        nonlinearity="relu",
        num_layers=1,
        bias=True,
        dropout=0.0,
        bidirectional=False,
        return_hidden=False,
        init_criterion="glorot",
        weight_init="complex",
    ):
        super().__init__()
        self.hidden_size = hidden_size * 2  # z = x + iy
        self.nonlinearity = nonlinearity
        self.num_layers = num_layers
        self.bias = bias
        self.dropout = dropout
        self.bidirectional = bidirectional
        self.reshape = False
        self.return_hidden = return_hidden
        self.init_criterion = init_criterion
        self.weight_init = weight_init

    def init_params(self, first_input):
        """
        Initializes the parameters of the RNN.

        Arguments
        ---------
        first_input : tensor
            A first input used for initializing the parameters.
        """
        if len(first_input.shape) > 3:
            self.reshape = True

        # Computing the feature dimensionality
        self.fea_dim = torch.prod(torch.tensor(first_input.shape[2:])) // 2
        self.batch_size = first_input.shape[0]
        self.device = first_input.device

        self.rnn = self._init_layers()

        self.rnn.to(first_input.device)

    def _init_layers(self,):
        """
        Initializes the layers of the ComplexRNN.

        Arguments
        ---------
        first_input : tensor
            A first input used for initializing the parameters.
        """
        rnn = torch.nn.ModuleList([])
        current_dim = self.fea_dim

        for i in range(self.num_layers):
            rnn_lay = ComplexRNN_Layer(
                current_dim,
                self.hidden_size,
                self.num_layers,
                self.batch_size,
                dropout=self.dropout,
                nonlinearity=self.nonlinearity,
                bidirectional=self.bidirectional,
                device=self.device,
                init_criterion=self.init_criterion,
                weight_init=self.weight_init,
            ).to(self.device)

            rnn.append(rnn_lay)

            if self.bidirectional:
                current_dim = self.hidden_size * 2
            else:
                current_dim = self.hidden_size

        return rnn

    def forward(self, x, hx=None, init_params=False):
        """Returns the output of the vanilla RNN.

        Arguments
        ---------
        x : torch.Tensor
        """
        if init_params:
            self.init_params(x)

        # Reshaping input tensors for 4d inputs
        if self.reshape:
            if len(x.shape) == 4:
                x = x.reshape(x.shape[0], x.shape[1], x.shape[2] * x.shape[3])

        output, hh = self._forward_rnn(x, hx=hx)

        if self.return_hidden:
            return output, hh
        else:
            return output

    def _forward_rnn(self, x, hx):
        """Returns the output of the vanilla ComplexRNN.

        Arguments
        ---------
        x : torch.Tensor
        """
        h = []
        if hx is not None:
            if self.bidirectional:
                hx = hx.reshape(
                    self.num_layers, self.batch_size * 2, self.hidden_size
                )

        # Processing the different layers
        for i, rnn_lay in enumerate(self.rnn):
            if hx is not None:
                x = rnn_lay(x, hx=hx[i])
            else:
                x = rnn_lay(x, hx=None)
            h.append(x[:, -1, :])
        h = torch.stack(h, dim=1)

        if self.bidirectional:
            h = h.reshape(h.shape[1] * 2, h.shape[0], self.hidden_size)
        else:
            h = h.transpose(0, 1)

        return x, h


class ComplexRNN_Layer(torch.jit.ScriptModule):
    """ This function implements complex-valued recurrent layer.

    Arguments
    ---------
    input_size: int
        Feature dimensionality of the input tensors.
    batch_size: int
        Batch size of the input tensors.
    hidden_size: int
        Number of output values.
    num_layers: int, optional
        Default: 1
        Number of layers to employ in the RNN architecture.
    nonlinearity: str, optional
        Default: tanh
        Type of nonlinearity (tanh, relu).
    dropout: float, optional
        Default: 0.0
        It is the dropout factor (must be between 0 and 1).
    device: str, optional
        Default: cpu
        Device used for running the computations (e.g, 'cpu', 'cuda').
    bidirectional: bool, optional
        Default: False
        If True, a bidirectioal model that scans the sequence both
        right-to-left and left-to-right is used.
    init_criterion: str , optional
        Default: he.
        (glorot, he).
        This parameter controls the initialization criterion of the weights.
        It is combined with weights_init to build the initialization method of
        the complex-valued weights.
    weight_init: str, optional
        Default: complex.
        (complex, unitary).
        This parameter defines the initialization procedure of the
        complex-valued weights. "complex" will generate random complex-valued
        weights following the init_criterion and the complex polar form.
        "unitary" will normalize the weights to lie on the unit circle.
        More details in: "Deep Complex Networks", Trabelsi C. et al.
    """

    def __init__(
        self,
        input_size,
        hidden_size,
        num_layers,
        batch_size,
        dropout=0.0,
        nonlinearity="relu",
        normalization="batchnorm",
        bidirectional=False,
        device="cpu",
        init_criterion="glorot",
        weight_init="complex",
    ):

        super(ComplexRNN_Layer, self).__init__()
        self.hidden_size = int(hidden_size)
        self.input_size = int(input_size)
        self.batch_size = batch_size
        self.bidirectional = bidirectional
        self.dropout = dropout
        self.device = device
        self.init_criterion = init_criterion
        self.weight_init = weight_init

        self.w = complex_linear(
            self.input_size,
            self.hidden_size,
            bias=True,
            weight_init=self.weight_init,
            init_criterion=self.init_criterion,
        ).to(device)

        self.u = complex_linear(
            self.hidden_size,
            self.hidden_size,
            bias=True,
            weight_init=self.weight_init,
            init_criterion=self.init_criterion,
        )

        if self.bidirectional:
            self.batch_size = self.batch_size * 2

        # Initial state
        self.h_init = torch.zeros(
            1, self.hidden_size * 2, requires_grad=False, device=self.device,
        )

        # Preloading dropout masks (gives some speed improvement)
        self._init_drop(self.batch_size)

        # Initilizing dropout
        self.drop = torch.nn.Dropout(p=self.dropout, inplace=False).to(device)

        self.drop_mask_te = torch.tensor([1.0], device=self.device).float()

        # Setting the activation function
        if nonlinearity == "tanh":
            self.act = torch.nn.Tanh().to(device)
        else:
            self.act = torch.nn.ReLU().to(device)

    @torch.jit.script_method
    def forward(self, x, hx=None):
        # type: (Tensor, Optional[Tensor]) -> Tensor # noqa F821
        """Returns the output of the ComplexRNN_layer.

        Arguments
        ---------
        x : torch.Tensor
        """
        if self.bidirectional:
            x_flip = x.flip(1)
            x = torch.cat([x, x_flip], dim=0)

        # Change batch size if needed
        self._change_batch_size(x)

        # Feed-forward affine transformations (all steps in parallel)
        w = self.w(x)

        # Processing time steps
        if hx is not None:
            h = self._complexrnn_cell(w, hx)
        else:
            h = self._complexrnn_cell(w, self.h_init)

        if self.bidirectional:
            h_f, h_b = h.chunk(2, dim=0)
            h_b = h_b.flip(1)
            h = torch.cat([h_f, h_b], dim=2)

        return h

    @torch.jit.script_method
    def _complexrnn_cell(self, w, ht):
        """Returns the hidden states for each time step.

        Arguments
        ---------
        wx : torch.Tensor
            Linearly transformed input.
        """
        hiddens = []

        # Sampling dropout mask
        drop_mask = self._sample_drop_mask()

        # Loop over time axis
        for k in range(w.shape[1]):
            at = w[:, k] + self.u(ht)
            ht = self.act(at) * drop_mask
            hiddens.append(ht)

        # Stacking hidden states
        h = torch.stack(hiddens, dim=1)
        return h

    def _init_drop(self, batch_size):
        """Initializes the recurrent dropout operation. To speed it up,
        the dropout masks are sampled in advance.
        """
        self.drop = torch.nn.Dropout(p=self.dropout, inplace=False).to(
            self.device
        )
        self.drop_mask_te = torch.tensor([1.0], device=self.device).float()

        self.N_drop_masks = 16000
        self.drop_mask_cnt = 0

        self.drop_masks = self.drop(
            torch.ones(
                self.N_drop_masks, self.hidden_size * 2, device=self.device,
            )
        ).data

    @torch.jit.script_method
    def _sample_drop_mask(self,):
        """Selects one of the pre-defined dropout masks
        """
        if self.training:

            # Sample new masks when needed
            if self.drop_mask_cnt + self.batch_size > self.N_drop_masks:
                self.drop_mask_cnt = 0
                self.drop_masks = (
                    self.drop(torch.ones(self.N_drop_masks, self.hidden_size,))
                    .to(self.device)
                    .data
                )

            # Sampling the mask
            drop_mask = self.drop_masks[
                self.drop_mask_cnt : self.drop_mask_cnt + self.batch_size
            ]
            self.drop_mask_cnt = self.drop_mask_cnt + self.batch_size

        else:
            drop_mask = self.drop_mask_te

        return drop_mask

    @torch.jit.script_method
    def _change_batch_size(self, x):
        """This function changes the batch size when it is different from
        the one detected in the initialization method. This might happen in
        the case of multi-gpu or when we have different batch sizes in train
        and test. We also update the h_int and drop masks.
        """
        if self.batch_size != x.shape[0]:
            self.batch_size = x.shape[0]

            if self.training:
                self.drop_masks = self.drop(
                    torch.ones(
                        self.N_drop_masks,
                        self.hidden_size * 2,
                        device=self.device,
                    )
                ).data