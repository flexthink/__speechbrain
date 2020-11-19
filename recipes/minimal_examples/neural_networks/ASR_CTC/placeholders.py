"""
This file has placeholders for most blocks in the data loading pipeline.
We should replace these one by one as we get stuff done.
"""
import torch
import torchaudio

torchaudio.set_audio_backend("soundfile")

# Manually created label to index, index to label mappings
# NOTE: At the moment, these do not have the blank index.
ASR_example_label2ind = {
    "aa": 0,
    "ae": 1,
    "ah": 2,
    "ao": 3,
    "aw": 4,
    "ax": 5,
    "ay": 6,
    "b": 7,
    "ch": 8,
    "cl": 9,
    "d": 10,
    "dh": 11,
    "dx": 12,
    "eh": 13,
    "el": 14,
    "er": 15,
    "ey": 16,
    "f": 17,
    "g": 18,
    "hh": 19,
    "ih": 20,
    "iy": 21,
    "jh": 22,
    "k": 23,
    "l": 24,
    "m": 25,
    "n": 26,
    "ng": 27,
    "ow": 28,
    "oy": 29,
    "p": 30,
    "r": 31,
    "s": 32,
    "sh": 33,
    "t": 34,
    "th": 35,
    "uh": 36,
    "uw": 37,
    "v": 38,
    "w": 39,
    "y": 40,
    "z": 41,
    "sil": 42,
    "vcl": 43,
}

ASR_example_ind2label = {
    0: "aa",
    1: "ae",
    2: "ah",
    3: "ao",
    4: "aw",
    5: "ax",
    6: "ay",
    7: "b",
    8: "ch",
    9: "cl",
    10: "d",
    11: "dh",
    12: "dx",
    13: "eh",
    14: "el",
    15: "er",
    16: "ey",
    17: "f",
    18: "g",
    19: "hh",
    20: "ih",
    21: "iy",
    22: "jh",
    23: "k",
    24: "l",
    25: "m",
    26: "n",
    27: "ng",
    28: "ow",
    29: "oy",
    30: "p",
    31: "r",
    32: "s",
    33: "sh",
    34: "t",
    35: "th",
    36: "uh",
    37: "uw",
    38: "v",
    39: "w",
    40: "y",
    41: "z",
    42: "sil",
    43: "vcl",
}


# Audio transform
# This could possibly be hardcoded into the Dataset object.
def torchaudio_load(wavpath):
    wav, samplerate = torchaudio.load(wavpath)
    wav = wav.squeeze(0)  # flat tensor
    return wav


class ExampleCategoricalEncoder:
    # I did not understand the current CategoricalEncoder, so
    # now just using this super simple version.
    def __init__(self, label2ind, ind2label):
        self.label2ind = label2ind
        self.ind2label = ind2label

    def encode_list(self, x):
        return [self.label2ind[item] for item in x]

    def decode_list(self, x):
        return [self.ind2label[item] for item in x]


def pad_and_stack(sequences, padding_value=0):
    # Basic padding. But something like this should be enough usually.
    num = len(sequences)
    lens = [s.size(0) for s in sequences]
    max_len = max(lens)
    out_dims = (num, max_len)
    out_tensor = sequences[0].data.new(*out_dims).fill_(padding_value)
    for i, tensor in enumerate(sequences):
        length = tensor.size(0)
        out_tensor[i, :length] = tensor
    relative_lens = torch.tensor(lens, dtype=torch.float) / max_len
    return out_tensor, relative_lens


def ASR_example_collation(batch):
    # This is coupled to the ASRMinimalExampleDataset output format.
    IDs, wavs, phns = zip(*((ex["id"], ex["wav"], ex["phn"]) for ex in batch))
    wavs, wav_lens = pad_and_stack(wavs)
    phns, phn_lens = pad_and_stack(phns)
    # And the output here defines our batch format.
    # For now, using the old format.
    return (IDs, wavs, wav_lens), (IDs, phns, phn_lens)