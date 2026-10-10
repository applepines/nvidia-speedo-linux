# nvidia-speedo-linux

A simple Linux CLI that reads NVIDIA GPU **Speedo** values and calculates **ASIC Quality (%)**
from their VFE minimum/maximum ranges.
It queries cached fuse information through the NVIDIA driver using only the Python standard library.

## Usage

Download the repository and run from its directory:

```bash
python3 read_speedo.py
```

Print JSON or save the raw VFE responses:

```bash
python3 read_speedo.py --json
python3 read_speedo.py --json > result.json
python3 read_speedo.py --dump-dir dumps
```

## Requirements

- 64-bit Linux and Python 3.9 or newer
- A running NVIDIA driver with its GSP firmware installed
- Read/write access to the NVIDIA device nodes and read access to the GSP firmware

No additional Python packages are required. All three cards listed below were also
queried successfully as a normal user, without `sudo`. If your system denies access,
retry with `sudo python3 read_speedo.py`.

## Verified Results

These values were read using NVIDIA Open Kernel Module / GSP **610.43.02**.

| GPU | Speedo | VFE Range | ASIC Quality (calculated) |
|---|---:|---:|---:|
| CMP 170HX | 1826 | 1695.34–1955.34 | 50.25% |
| RTX 3090 | 1884 | 1708.27–2008.69 | 58.49% |
| RTX 3090 Ti | 1870 | 1743.02–2013.02 | 47.03% |

[Example JSON output](examples/result.json)

## How It Works

The tool calls two RM commands through `/dev/nvidiactl`:

| Command | Purpose |
|---|---|
| `0x2080A0B1` — VFE GET_INFO | Read the Speedo definition and cached integer value |
| `0x2080A0B3` — VFE GET_CONTROL | Read the VFE minimum and maximum range |

The Speedo entry is identified by `VFIELD_ID_S_PARAM = 0x07`. GPU models, entry indices,
register addresses, and per-card values and ranges are not hardcoded.
Buffer sizes are read from method descriptors in the installed GSP firmware.

The tool does not write GPU registers or change voltages or clocks.

## ASIC Quality

```text
ASIC Quality (%) = 100 × (Speedo − minimum) / (maximum − minimum)
```

This percentage expresses the Speedo value's position within its VFE range.
It is not a population percentile or GPU health score.

The CLI labels this value `normalized`; JSON uses `range_normalized_percent`.

## Compatibility

The current private VFE record layout was verified on **610.43.02**. Compatibility with
other GPUs or driver versions has not been verified. Unknown layouts are reported as errors.
Field offsets are defined in [`layout.json`](layout.json).

You can also specify the firmware and layout files explicitly:

```bash
python3 read_speedo.py --firmware /path/to/gsp.bin --layout /path/to/layout.json
```

## References

Based on NVIDIA's public RM ABI and VFE definitions, with query mappings from
[nvapi-rs](https://github.com/Skyworks-Neo/nvapi-rs) and
[nvoc](https://github.com/Skyworks-Neo/nvoc). The Linux query implementation is written in Python.

## License

[MIT](LICENSE)
