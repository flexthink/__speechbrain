"""SpeechBrain Extended CSV Compatibility"""
from speechbrain.data_io.dataset import DynamicItemDataset
import soundfile as sf
import collections
import csv
import pickle
import logging
import torch
import re

logger = logging.getLogger(__name__)


SF_FORMATS = sf.available_formats()
ITEM_POSTFIX = "_data"

CSVItem = collections.namedtuple("CSVItem", ["data", "format", "opts"])
# The Legacy Extended CSV Data item triplet


class ExtendedCSVDataset(DynamicItemDataset):
    """Extended CSV compatibility for DynamicItemDataset

    Uses the SpeechBrain Extended CSV data format, where the
    CSV must have an 'ID' and 'duration' fields.

    The rest of the fields come in triplets:
    a <name>, <name>_format, <name>_opts

    These add a <name>_sb_data item in the dict. Additionally, a
    basic DynamicItem (see DynamicItemDataset) is created, which
    loads the _sb_data item.

    Bash-like string replacements with $to_replace are supported.

    NOTE
    ----
    Mapping from legacy interface:
        sentence_sorting -> sorting
        avoid_if_shorter_than -> min_duration
        avoid_if_longer_than -> max_duration
        csv_read -> output_keys, and if you want ids add "id" as key

    Arguments
    ---------
    csvpath : str, path
        Path to extended CSV.
    replacements : dict
        Used for Bash-like $-prefixed substitution,
        e.g. {"data_folder": "/home/speechbrain/data"}
        $data_folder/utt1.wav -> /home/speechbain/data/utt1.wav
    sorting : {"original", "ascending", "descending", "random"}
        Keep CSV order, or sort ascending or descending by duration,
        or shuffle. NOTE: shuffled order is not reproducible.
        It is here for backwards compatibility.
    min_duration : float, int
        Minimum duration in seconds. Discards other entries.
    max_duration : float, int
        Maximum duration in seconds. Discards other entries.
    dynamic_items : dict
        Configuration for extra dynamic items produced when fetching an
        example. Nested dict with the format (in YAML notation):
        <key>:
            func: <callable> # To be called
            argkeys: <list> # keys of args, either other funcs or in data
        <key2>: ...
        NOTE: A dynamic item is automatically added for each CSV data-triplet
    output_keys : list, None
        The list of output keys to produce. You can refer to names of the
        CSV data-triplets. E.G. if the CSV has: wav,wav_format,wav_opts,
        then the Dataset has a dynamic item output available with key "wav"
        NOTE: If None, read all existing.
    """

    def __init__(
        self,
        csvpath,
        replacements={},
        sorting="original",
        min_duration=0,
        max_duration=36000,
        dynamic_items=None,
        output_keys=None,
    ):
        if sorting not in ["original", "ascending", "descending"]:
            clsname = self.__class__.__name__
            raise ValueError(f"{clsname} doesn't support {sorting} sorting")
        # Load the CSV, init class
        data, di_to_add, data_names = load_sb_extended_csv(
            csvpath, replacements
        )
        super().__init__(data, dynamic_items, output_keys)
        for key, func, argname in di_to_add:
            self.add_dynamic_item(key, func, argname)
        # Handle filtering, sorting:
        reverse = False
        sort_key = None
        if sorting == "ascending" or "descending":
            sort_key = "duration"
        if sorting == "descending":
            reverse = True
        filtered_sorted_ids = self._filtered_sorted_ids(
            key_min_value={"duration": min_duration},
            key_max_value={"duration": max_duration},
            sort_key=sort_key,
            reverse=reverse,
        )
        self.data_ids = filtered_sorted_ids
        # Handle None output_keys (differently than Base)
        if output_keys is None:
            self.set_output_keys(data_names)


def load_sb_extended_csv(csv_path, replacements={}):
    """Loads SB Extended CSV and formats string values

    Uses the SpeechBrain Extended CSV data format, where the
    CSV must have an 'ID' and 'duration' fields.

    The rest of the fields come in triplets:
    a <name>, <name>_format, <name>_opts

    These add a <name>_sb_data item in the dict. Additionally, a
    basic DynamicItem (see DynamicItemDataset) is created, which
    loads the _sb_data item.

    Bash-like string replacements with $to_replace are supported.

    This format has its restriction, but they allow some tasks to
    have loading specified by the CSV.

    Arguments
    ----------
    csv_path : str
        Path to CSV file
    replacements : dict
        Optional dict:
        e.g. {"data_folder": "/home/speechbrain/data"}
        This is used to recursively format all string values in the data

    Returns
    -------
    dict
        CSV data with replacements applied
    list
        List of DynamicItems to add in DynamicItemDataset

    """
    with open(csv_path, newline="") as csvfile:
        result = {}
        reader = csv.DictReader(csvfile, skipinitialspace=True)
        variable_finder = re.compile(r"\$([\w.]+)")
        if not reader.fieldnames[0] == "ID":
            raise KeyError(
                "CSV has to have an 'ID' field, with unique ids"
                " for all data points"
            )
        if not reader.fieldnames[1] == "duration":
            raise KeyError(
                "CSV has to have an 'duration' field, "
                "with the length of the data point in seconds."
            )
        if not len(reader.fieldnames[2:]) % 3 == 0:
            raise ValueError(
                "All named fields must have 3 entries: "
                "<name>, <name>_format, <name>_opts"
            )
        names = reader.fieldnames[2::3]
        for row in reader:
            # Make a triplet for each name
            data_point = {}
            # ID:
            data_id = row["ID"]
            del row["ID"]  # This is used as a key in result, instead.
            # Duration:
            data_point["duration"] = float(row["duration"])
            del row["duration"]  # This is handled specially.
            if data_id in result:
                raise ValueError(f"Duplicate id: {data_id}")
            # Replacements:
            # Only need to run these in the actual data,
            # not in _opts, _format
            for key, value in list(row.items())[::3]:
                try:
                    row[key] = variable_finder.sub(
                        lambda match: replacements[match[1]], value
                    )
                except KeyError:
                    raise KeyError(
                        f"The item {value} requires replacements "
                        "which were not supplied."
                    )
            for i, name in enumerate(names):
                triplet = CSVItem(*list(row.values())[i : i + 3])
                data_point[name + ITEM_POSTFIX] = triplet
            result[data_id] = data_point
        # Make a DynamicItem for each CSV entry
        # _read_csv_item delegates reading to further
        dynamic_items_to_add = [
            (name, _read_csv_item, name + ITEM_POSTFIX) for name in names
        ]
        return result, dynamic_items_to_add, names


def _read_csv_item(item):
    """Reads the different formats supported in SB Extended CSV

    Delegates to the relevant functions.
    """
    opts = _parse_csv_item_opts(item.opts)
    if item.format.upper() in SF_FORMATS:
        return read_wav_soundfile(item.data, opts)
    elif item.format == "pkl":
        return read_pkl(item.data, opts)
    elif item.format == "string":
        # Just implement string reading here.
        # NOTE: No longer supporting
        # lab2ind mapping like before.
        # Try decoding string
        string = item.data
        try:
            string = string.decode("utf-8")
        except AttributeError:
            pass
        # Splitting elements with ' '
        string = string.split(" ")
        return string
    else:
        raise TypeError(f"Don't know how to read {item.format}")


def _parse_csv_item_opts(entry):
    """Parse the _opts field in a SB Extended CSV item"""
    # Accepting even slightly weirdly formatted entries:
    entry = entry.strip()
    if len(entry) == 0:
        return {}
    opts = {}
    for opt in entry.split(" "):
        opt_name, opt_val = opt.split(":")
        opts[opt_name] = opt_val
    return opts


# TODO: Consider making less complex
def read_wav_soundfile(file, data_options={}, lab2ind=None):  # noqa: C901
    """
    Read wav audio files with soundfile.

    This supports the SB Extended CSV audio reading.

    Arguments
    ---------
    file : str
        The filepath to the file to read
    data_options : dict
        a dictionary containing options for the reader.
    lab2ind : dict, None
        a dictionary for converting labels to indices

    Returns
    -------
    numpy.array
        An array with the read signal

    Example
    -------
    >>> read_wav_soundfile('samples/audio_samples/example1.wav')[0:2]
    array([0.00024414, 0.00018311], dtype=float32)
    """
    # Option initialization
    start = 0
    stop = None
    endian = None
    subtype = None
    channels = None
    samplerate = None

    # List of possible options
    possible_options = [
        "start",
        "stop",
        "samplerate",
        "endian",
        "subtype",
        "channels",
    ]

    # Check if the specified options are supported
    for opt in data_options:
        if opt not in possible_options:

            err_msg = "%s is not a valid options. Valid options are %s." % (
                opt,
                possible_options,
            )

            logger.error(err_msg, exc_info=True)

    # Managing start option
    if "start" in data_options:
        try:
            start = int(data_options["start"])
        except Exception:

            err_msg = (
                "The start value for the file %s must be an integer "
                "(e.g start:405)" % (file)
            )

            logger.error(err_msg, exc_info=True)

    # Managing stop option
    if "stop" in data_options:
        try:
            stop = int(data_options["stop"])
        except Exception:

            err_msg = (
                "The stop value for the file %s must be an integer "
                "(e.g stop:405)" % (file)
            )

            logger.error(err_msg, exc_info=True)

    # Managing samplerate option
    if "samplerate" in data_options:
        try:
            samplerate = int(data_options["samplerate"])
        except Exception:

            err_msg = (
                "The sampling rate for the file %s must be an integer "
                "(e.g samplingrate:16000)" % (file)
            )

            logger.error(err_msg, exc_info=True)

    # Managing endian option
    if "endian" in data_options:
        endian = data_options["endian"]

    # Managing subtype option
    if "subtype" in data_options:
        subtype = data_options["subtype"]

    # Managing channels option
    if "channels" in data_options:
        try:
            channels = int(data_options["channels"])
        except Exception:

            err_msg = (
                "The number of channels for the file %s must be an integer "
                "(e.g channels:2)" % (file)
            )

            logger.error(err_msg, exc_info=True)

    # Reading the file with the soundfile reader
    try:
        [signal, fs] = sf.read(
            file,
            start=start,
            stop=stop,
            samplerate=samplerate,
            endian=endian,
            subtype=subtype,
            channels=channels,
        )

        signal = signal.astype("float32")

    except RuntimeError as e:
        err_msg = "cannot read the wav file %s" % (file)
        e.args = (*e.args, err_msg)
        raise

    # Set time_steps always last as last dimension
    if len(signal.shape) > 1:
        signal = signal.transpose()

    return signal


def read_pkl(file, data_options={}, lab2ind=None):
    """
    This function reads tensors store in pkl format.

    Arguments
    ---------
    file : str
        The path to file to read.
    data_options : dict, optional
        A dictionary containing options for the reader.
    lab2ind : dict, optional
        Mapping from label to integer indices.

    Returns
    -------
    numpy.array
        The array containing the read signal
    """

    # Trying to read data
    try:
        with open(file, "rb") as f:
            pkl_element = pickle.load(f)
    except pickle.UnpicklingError:
        err_msg = "cannot read the pkl file %s" % (file)
        raise ValueError(err_msg)

    type_ok = False

    if isinstance(pkl_element, list):

        if isinstance(pkl_element[0], float):
            tensor = torch.FloatTensor(pkl_element)
            type_ok = True

        if isinstance(pkl_element[0], int):
            tensor = torch.LongTensor(pkl_element)
            type_ok = True

        if isinstance(pkl_element[0], str):

            # convert string to integer as specified in self.label_dict
            if lab2ind is not None:
                for index, val in enumerate(pkl_element):
                    pkl_element[index] = lab2ind[val]

            tensor = torch.LongTensor(pkl_element)
            type_ok = True

        if not (type_ok):
            err_msg = (
                "The pkl file %s can only contain list of integers, "
                "floats, or strings. Got %s"
            ) % (file, type(pkl_element[0]))
            raise ValueError(err_msg)
    else:
        tensor = pkl_element

    tensor_type = tensor.dtype

    # Conversion to 32 bit (if needed)
    if tensor_type == "float64":
        tensor = tensor.astype("float32")

    if tensor_type == "int64":
        tensor = tensor.astype("int32")

    return tensor