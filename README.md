
# jdupes-sidecar.py

**jdupes-sidecar.py** is a Python script that extends [jdupes](https://jdupes.com) file deduplication features (on Linux systems) with sidecar file functionalities. It extends `jdupes` functionality by:

- Preserving the order of directories when deciding which duplicates to keep.
- Creating and managing sidecar files that record the paths of deleted duplicates.
- Offering dry run mode to preview actions without making changes.
- Providing customizable behavior for handling existing sidecar files and deleting sidecar files of deleted duplicates.
- Allowing custom paths for `jdupes` binary and hash database.

**This script takes destructive actions. Do not trust it. This wrapper was originally designed to fit my particular use case. Read and understand the code before using it. Usage of this script is at your own risk. Always back up important data before performing any operations.**

## Table of Contents

- [Features](#features)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Usage](#usage)
  - [Basic Usage](#basic-usage)
  - [Options](#options)
  - [Examples](#examples)
- [Notes and Considerations](#notes-and-considerations)
- [Contributing](#contributing)
- [License](#license)

## Features

- **Directory Order Preservation**: Uses `--param-order` with `jdupes` to consider the order of directories provided when deciding which duplicate file to keep.
- **Sidecar Files**: Creates a `.dupes` sidecar file alongside the surviving file, listing the full paths of the deleted duplicates.
- **Sidecar Merging**: Merges existing sidecar files from deleted duplicates into the surviving file's sidecar file (default behavior).
- **Sidecar Deletion**: Deletes the sidecar files of deleted duplicates after merging them (default behavior, can be disabled with `--no-delete-duplicate-sidecar`).
- **Dry Run Mode**: Allows you to preview actions without deleting files or creating sidecar files.
- **Verbosity Levels**: Adjustable verbosity to control the amount of output.
- **Progress Display**: Shows real-time progress from `jdupes` and progress bars for processing duplicates.
- **Customizable `jdupes` Options**: Specify custom paths for `jdupes` binary and hash database.
- **Merge/Do Not Merge Existing Sidecars**: Choose whether to merge existing sidecar files of duplicate files with the surviving file's sidecar (can be disabled with `--no-merge-existing-sidecars`).

## Prerequisites

- **Python 3**: The script requires Python 3.6 or newer.
- **jdupes**: Ensure you have `jdupes` installed on your system. Tested with jdupes-1.27.3.
- **tqdm Module**: Used for displaying progress bars. Install via `apt` or `pip`. Tested with tqdm==4.66.4.

## Installation

### 1. Install `jdupes`

Follow the instructions on the [jdupes Codeberg repository](https://codeberg.org/jbruchon/jdupes).

### 2. Install `tqdm` Module

Install `tqdm` using `apt` (recommended):

```bash
sudo apt install python3-tqdm
```

Alternatively, install using `pip`:

```bash
pip3 install tqdm
```

### 3. Download the Script

Clone this repository or download `jdupes-sidecar.py` directly:

```bash
wget https://github.com/yourusername/yourrepository/jdupes-sidecar.py
```

### 4. Make the Script Executable

```bash
chmod +x jdupes-sidecar.py
```

## Usage

### Basic Usage

```bash
./jdupes-sidecar.py [options] /path/to/dir1 /path/to/dir2 ...
```

Provide the directories you want to deduplicate as arguments. The order of directories determines which duplicates are kept (first directory has the highest priority).

### Options

- `-n`, `--dry-run`: Perform a dry run without deleting files or creating sidecar files.
- `-o OUTPUT`, `--output OUTPUT`: Output file for dry run report (default: `dry_run_output.txt`).
- `-v`, `--verbose`: Increase output verbosity. Can be used multiple times (`-v`, `-vv`, `-vvv`).
- `--progress`: Display progress information.
- `--jdupes-path JDUPES_PATH`: Path to the `jdupes` binary (default: `jdupes`).
- `--jdupes-hashdb JDUPES_HASHDB`: Path to the hash database file to be used by `jdupes`. See [WARNING](#notes-and-considerations) below.
- `--no-merge-existing-sidecars`: Do not merge existing sidecar files from deletion candidates.
- `--no-delete-duplicate-sidecar`: Do not delete the sidecar files of deleted duplicate files after merging.
- `-h`, `--help`: Show help message and exit.

### Examples

#### 1. Dry Run Mode with Default Verbosity

```bash
./jdupes-sidecar.py -n /path/to/dir1 /path/to/dir2
```

- Performs a dry run.
- Outputs actions to `dry_run_output.txt`.
- No files are deleted or modified.

#### 2. Normal Mode with Confirmation and Verbosity

```bash
./jdupes-sidecar.py -v /path/to/dir1 /path/to/dir2
```

- Runs in normal mode.
- Asks for confirmation before proceeding.
- Outputs informational messages due to `-v`.

#### 3. Normal Mode with Progress Display

```bash
./jdupes-sidecar.py --progress /path/to/dir1 /path/to/dir2
```

- Displays real-time progress from `jdupes`.
- Shows a progress bar for processing duplicates.

#### 4. Specify Custom `jdupes` Path and Hash Database

```bash
./jdupes-sidecar.py --jdupes-path /usr/local/bin/jdupes --jdupes-hashdb /var/hashdb.txt /path/to/dir1 /path/to/dir2
```

- Uses a custom `jdupes` binary.
- Specifies a hash database for `jdupes`.

#### 5. Full Command with All Options

```bash
./jdupes-sidecar.py --jdupes-path /usr/local/bin/jdupes --jdupes-hashdb /var/hashdb.txt --progress -vv -o report.txt /path/to/dir1 /path/to/dir2
```

- Uses custom `jdupes` path and hash database.
- Displays progress and sets verbosity to DEBUG level.
- Outputs dry run report to `report.txt`.

## Notes and Considerations

- **Directory Order Matters**: The order of directories provided determines which duplicates are kept. The first directory has the highest priority. This is because we pass --param-order by default.
- **Dry Run Mode**: Always recommended to perform a dry run first to verify actions.
- **Verbosity Levels**:
  - No `-v`: Warnings and errors are displayed.
  - `-v`: Informational messages are included.
  - `-vv` or higher: Detailed debug information is shown.
- **Progress Display**: Requires `tqdm` module. Shows real-time progress from `jdupes` and script processing.
- **Permissions**:
  - Ensure you have read/write/delete permissions for the specified directories.
  - When running with `sudo`, make sure `tqdm` is installed for the root user.
- **Hash Database**:
  - Use `--jdupes-hashdb` to specify a hash database file for `jdupes`.
  - Improves performance for large datasets.
  - Ensure you have permissions to read/write the hash database file.
  - **WARNING: Make sure you understand the quirks, limitations, and expirimental nature of this jdupes feature.**
    - See the special notes for this function underneath the table at: https://codeberg.org/jbruchon/jdupes#usage
- **Safety Measures**:
  - The script prompts once for initial confirmation in normal mode.
  - Includes error handling for file operations and `jdupes` execution.
- **Dependencies**:
  - Requires `jdupes` to be installed and accessible.
  - Requires `tqdm` module for progress display.

## Contributing

Contributions are welcome!

## License

See the [LICENSE](LICENSE) file for details.
