# jdupes-sidecar.py

**jdupes-sidecar.py** is a Python script that extends [jdupes](https://codeberg.org/jbruchon/jdupes) (on Linux systems) functionality by:

- Creating and managing sidecar files that record the paths of deleted duplicates.
- Providing customizable behavior for handling existing sidecar files and deleting sidecar files of deleted duplicates.
- Offering dry run mode to preview actions without making changes.
- Preserving the order of directories when deciding which duplicates to keep.
- Providing direct parameters for custom paths to the `jdupes` binary and hash database.

**This script takes destructive actions. Do not trust it. This wrapper was originally designed to fit my particular use case. Read and understand the code before using it. Usage of this script is at your own risk. Always back up important data before performing any operations.**

## Table of Contents

- [How It Works](#how-it-works)
- [Features](#features)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Usage](#usage)
  - [Basic Usage](#basic-usage)
  - [Options](#options)
  - [Examples](#examples)
- [Safety Checks](#safety-checks)
- [Notes and Considerations](#notes-and-considerations)
- [Exit Status](#exit-status)
- [Running the Tests](#running-the-tests)
- [Contributing](#contributing)
- [License](#license)

## How It Works

1. `jdupes` scans the directories you give and reports sets of identical files.
2. In each set, the script keeps the file from the earliest directory on the command line and deletes the others.
3. The full path of every deleted duplicate is appended to a sidecar file next to the kept file, named after it plus `.dupes`.
4. If a deleted duplicate had its own sidecar file (from an earlier run), its entries are merged into the kept file's sidecar, so the history of where copies used to live is never lost.

For example, after `./jdupes-sidecar.py /photos /backup/photos /old-laptop` finds three copies of `IMG_0001.jpg`, only `/photos/IMG_0001.jpg` remains, and `/photos/IMG_0001.jpg.dupes` contains:

```
/backup/photos/IMG_0001.jpg
/old-laptop/IMG_0001.jpg
```

Sidecar files are plain text with one absolute path per line. Paths are recorded with symlinked directories resolved (so `~/Pictures/x.jpg` is recorded as `/mnt/data/Pictures/x.jpg` if `~/Pictures` is a symlink to `/mnt/data/Pictures`), and file names are written byte-for-byte as they are on disk.

## Features

- **Directory Order Preservation**: The order of directories on the command line decides which copy is kept: the copy in the first directory wins. Within one directory, the copy `jdupes` lists first is kept (by name, unless you pass `--order=time` through `--jdupes-extra-args`).
- **Sidecar Files**: Creates a `.dupes` sidecar file alongside the surviving file, listing the full paths of the deleted duplicates.
- **Sidecar Merging**: Merges existing sidecar files from deleted duplicates into the surviving file's sidecar file (default behavior, can be disabled with `--no-merge-existing-sidecars`).
- **Sidecar Deletion**: Deletes the sidecar files of deleted duplicates after merging them (default behavior, can be disabled with `--no-delete-duplicate-sidecar`).
- **Dry Run Mode**: Writes a report of every planned action without deleting or writing anything.
- **Confirmation With a Summary**: Scans first, shows how many files would be deleted and how much space that reclaims, then asks before deleting anything. `--yes` skips the question for unattended runs.
- **Any File Name**: File names that are not valid UTF-8 are handled correctly.
- **Verbosity Levels**: Adjustable verbosity to control the amount of output.
- **Progress Display**: Shows real-time progress from `jdupes`, and a progress bar for processing duplicates if `tqdm` is installed.
- **Customizable `jdupes` Options**: Specify the `jdupes` binary, a hash database, and extra `jdupes` options.

## Prerequisites

- **Python 3**: Python 3.8 or newer.
- **jdupes**: Ensure you have `jdupes` installed on your system. Tested with jdupes 1.27.3 and 1.31.2.
- **tqdm Module** (optional): Draws the `--progress` bar. Tested with tqdm 4.66.

## Installation

### 1. Install `jdupes`

Install it from your distribution (`sudo apt install jdupes` on Debian and Ubuntu), or follow the instructions on the [jdupes Codeberg repository](https://codeberg.org/jbruchon/jdupes).

### 2. Install `tqdm` Module (optional)

Only needed for the `--progress` bar. Install `tqdm` using `apt` (recommended):

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
git clone https://github.com/shawnbarton/jdupes-sidecar.git
# or
wget https://raw.githubusercontent.com/shawnbarton/jdupes-sidecar/master/jdupes-sidecar.py
```

### 4. Make the Script Executable

A clone is already executable. After a direct download, run:

```bash
chmod +x jdupes-sidecar.py
```

## Usage

### Basic Usage

```bash
./jdupes-sidecar.py [options] /path/to/dir1 /path/to/dir2 ...
```

Provide the directories you want to deduplicate as arguments (`jdupes` does not accept individual files). The order of directories determines which duplicates are kept (first directory has the highest priority). Directories may overlap (for example `/data` and `/data/archive`); a file counts as part of the first directory listed that contains it.

### Options

- `-n`, `--dry-run`: Report what would be done without deleting files or writing sidecar files.
- `-o OUTPUT`, `--output OUTPUT`: File for the dry run report (default: `dry_run_output.txt`). Use `-` to print the report instead.
- `--yes`: Do not ask for confirmation before deleting. Without it, the script asks once after the scan, and does nothing if there is no terminal to answer.
- `-v`, `--verbose`: Increase output verbosity. Can be used multiple times (`-v`, `-vv`).
- `--progress`: Display progress from `jdupes` while it scans, and a progress bar while duplicates are processed (the bar needs `tqdm`).
- `--jdupes-path JDUPES_PATH`: Path to the `jdupes` binary (default: `jdupes`).
- `--jdupes-hashdb JDUPES_HASHDB`: Path to the hash database file to be used by `jdupes`. See [the warning](#notes-and-considerations) below.
- `--jdupes-extra-args JDUPES_EXTRA_ARGS`: Extra options to pass through to `jdupes`, such as `--no-hidden` or `--zero-match`. Write it as `--jdupes-extra-args="..."`, with the `=`, so that a value starting with `-` is not mistaken for an option of this script. Only options that affect which files are compared are accepted; see [Notes and Considerations](#notes-and-considerations). (Formerly `--jdupes-extra-cmds`, which still works.)
- `--sidecar-extension SIDECAR_EXTENSION`: Extension of sidecar files (default: `.dupes`). It must be a single extension made of letters, digits, `_` or `-`, because `jdupes` can only exclude files by their last extension.
- `--no-exclude-sidecar`: Do not exclude sidecar files from the `jdupes` search. By default they are excluded with the `jdupes` option `--ext-filter=noext:EXTENSION`, which ignores case.
- `--no-merge-existing-sidecars`: Do not merge existing sidecar files of deleted duplicates; leave them where they are.
- `--no-delete-duplicate-sidecar`: Do not delete the sidecar files of deleted duplicates after merging them.
- `-h`, `--help`: Show help message and exit.

### Examples

#### 1. Dry Run

```bash
./jdupes-sidecar.py -n /path/to/dir1 /path/to/dir2
```

- Writes every planned action to `dry_run_output.txt`.
- No files are deleted or modified.

#### 2. Normal Mode with Confirmation and Verbosity

```bash
./jdupes-sidecar.py -v /path/to/dir1 /path/to/dir2
```

- Scans, prints a summary of what would be deleted, and asks for confirmation.
- Lists each deleted file and sidecar change due to `-v`.

#### 3. Unattended Run with Progress Display

```bash
./jdupes-sidecar.py --yes --progress /path/to/dir1 /path/to/dir2
```

- Deletes duplicates without asking.
- Displays real-time progress from `jdupes`, and a progress bar for processing duplicates.

#### 4. Passing Options to jdupes

```bash
./jdupes-sidecar.py --jdupes-extra-args="--no-hidden --order=time" /path/to/dir1 /path/to/dir2
```

- Ignores hidden files and, within each directory, keeps the oldest copy.

## Safety Checks

Before deleting a duplicate, the script checks that:

- Neither the duplicate nor the kept file has changed since the scan (same size, modification time and inode), for example while the confirmation question was waiting for an answer.
- The duplicate is the same size as the kept file. A different size means one of them changed during the scan.
- The duplicate is not the kept file itself under another path, or a hard link to it. Such files are left alone: deleting them would reclaim no space, or would delete the kept file.
- If the duplicate has a sidecar file to merge, it can be read and looks like a sidecar file (one absolute path per line). Any other file that happens to have the sidecar name is left alone, along with the duplicate.
- Neither the kept file's sidecar file nor the duplicate's is itself one of the duplicates found (only possible with `--no-exclude-sidecar`).
- The duplicate's path contains no newline, which a sidecar file could not record.
- The kept file's sidecar file can be opened for writing. If not, nothing in that set is deleted.

A duplicate is only deleted if all of these hold. Its path is recorded in the sidecar file as soon as it is deleted; Ctrl-C and termination signals are held back until both have happened. Merged sidecar files are only deleted after their contents have been written. Anything skipped, other than another name for the kept file, is reported and makes the script exit with status 1.

## Notes and Considerations

- **Directory Order Matters**: The order of directories provided determines which duplicates are kept. The first directory has the highest priority.
- **Dry Run Mode**: Always recommended to perform a dry run first to verify actions.
- **Verbosity Levels**:
  - No `-v`: Warnings and errors are displayed.
  - `-v`: Each action taken is listed.
  - `-vv` or higher: Debug information, such as the `jdupes` command line, is also shown.
- **Options Passed to jdupes**: `--jdupes-extra-args` accepts only these `jdupes` options, which affect which files are compared and how: `-@`/`--loud`, `-0`/`--print-null`, `-1`/`--one-file-system`, `-A`/`--no-hidden`, `-C`/`--chunk-size`, `-H`/`--hard-links`, `-i`/`--reverse`, `-I`/`--isolate`, `-o`/`--order`, `-O`/`--param-order`, `-p`/`--permissions`, `-q`/`--quiet`, `-Q`/`--quick`, `-r`/`--recurse`, `-s`/`--symlinks`, `-t`/`--no-change-check`, `-U`/`--no-trav-check`, `-X`/`--ext-filter`, `-y`/`--hash-db`, `-z`/`--zero-match` and `-Z`/`--soft-abort`. Everything else is rejected, because it would make `jdupes` act on files itself and bypass the sidecar files (such as `--delete` or `--link-hard`), change the output this script reads (such as `--json` or `--omit-first`), or make matches unsafe to delete (`--partial-only`). Arguments are read exactly as `jdupes` reads them, including abbreviated long options such as `--zero` and option values. Plain paths are rejected too: give directories as normal arguments instead. `--quick` prints a warning, since it skips the byte-for-byte comparison of files.
- **Permissions**:
  - Ensure you have read/write/delete permissions for the specified directories.
  - When running with `sudo` and `--progress`, make sure `tqdm` is installed for the root user.
- **Hash Database**:
  - Use `--jdupes-hashdb` to specify a hash database file for `jdupes`.
  - Improves performance for large datasets.
  - Ensure you have permissions to read/write the hash database file.
  - In our testing, the hash database does not work in `jdupes` 1.27.3 (the version packaged in Ubuntu 24.04): it crashes if the database file does not exist yet, and otherwise fails to save it. It works in 1.31.2. The script stops without changing anything when `jdupes` fails.
  - **WARNING: Make sure you understand the quirks, limitations, and experimental nature of this jdupes feature.**
    - See the special notes for this function underneath the table at: https://codeberg.org/jbruchon/jdupes#usage
- **File Names**: The script reads `jdupes` results with `--print-null` rather than `--json`, because `jdupes`' JSON output hangs or prints corrupted paths for file names that are not valid UTF-8 (still the case in jdupes 1.31.2).
- **Dependencies**:
  - Requires `jdupes` to be installed and accessible.
  - `tqdm` is only needed for the `--progress` bar.

## Exit Status

- `0`: Success, including when no duplicates are found or the user declines the confirmation.
- `1`: An error stopped the run, some duplicates could not be deleted or recorded (see the messages printed), or confirmation was needed but standard input was closed.
- `2`: Invalid command-line arguments.

## Running the Tests

The tests use only the Python standard library. Most of them run the script against real files with `jdupes`, and are skipped if `jdupes` is not installed.

```bash
python3 -m unittest discover tests
```

To test with a specific `jdupes` binary, set `JDUPES`:

```bash
JDUPES=/path/to/jdupes python3 -m unittest discover tests
```

## Contributing

Contributions are welcome!

## License

See the [LICENSE](LICENSE.md) file for details.
