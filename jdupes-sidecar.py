#!/usr/bin/env python3

import json
import sys
import os
import subprocess
import argparse
import logging
import shlex

# We need to import tqdm for progress bar
try:
    from tqdm import tqdm
except ImportError:
    print("This script requires the 'tqdm' module. Install it by running 'pip install tqdm'")
    sys.exit(1)


def get_directory_ordered_files(file_list, directories):
    """
    Sort the file_list according to the order of the provided directories.

    Args:
        file_list (list): List of file paths to be sorted.
        directories (list): List of directory paths in desired order.

    Returns:
        list: Sorted list of file paths.
    """
    # Normalize directory paths
    directories = [os.path.normpath(os.path.abspath(d)) for d in directories]
    # Normalize file paths
    normalized_file_list = [os.path.normpath(os.path.abspath(f)) for f in file_list]
    # Sort the file_list according to the directory order
    files_in_order = []
    for directory in directories:
        dir_files = [f for f in normalized_file_list if f.startswith(directory + os.path.sep)]
        files_in_order.extend(dir_files)
    # Add any files not in the directories list
    remaining_files = [f for f in normalized_file_list if f not in files_in_order]
    files_in_order.extend(remaining_files)
    return files_in_order


def main():
    parser = argparse.ArgumentParser(
        description='Deduplicate files using jdupes and manage duplicates via sidecar files.'
    )
    parser.add_argument('directories', nargs='+', help='Directories to deduplicate')
    parser.add_argument('-n', '--dry-run', action='store_true',
                        help='Perform a dry run without deleting files or creating sidecar files')
    parser.add_argument('-o', '--output', help='Output file for dry run report',
                        default='dry_run_output.txt')
    parser.add_argument('-v', '--verbose', action='count', default=0,
                        help='Increase output verbosity (can be used multiple times)')
    parser.add_argument('--progress', action='store_true', help='Display progress information')
    parser.add_argument('--jdupes-path', help='Path to the jdupes binary', default='jdupes')
    parser.add_argument('--jdupes-hashdb', help='Path to the hash database file to be used by jdupes')
    parser.add_argument('--jdupes-extra-cmds', help='Extra command-line arguments to pass to jdupes', default='')
    parser.add_argument('--sidecar-extension', help='File extension for sidecar files (default: .dupes)',
                        default='.dupes')
    parser.add_argument('--no-exclude-sidecar', action='store_true',
                        help='Do not exclude sidecar files from jdupes processing')
    parser.add_argument('--no-merge-existing-sidecars', action='store_true',
                        help='Do not merge existing sidecar files from deletion candidates')
    parser.add_argument('--no-delete-duplicate-sidecar', action='store_true',
                        help='Do not delete sidecar files of duplicate files after merging')
    args = parser.parse_args()

    directories = args.directories
    dry_run = args.dry_run
    output_file = args.output
    show_progress = args.progress
    jdupes_path = args.jdupes_path
    hash_db = args.jdupes_hashdb
    jdupes_extra_cmds = args.jdupes_extra_cmds
    sidecar_extension = args.sidecar_extension
    no_exclude_sidecar = args.no_exclude_sidecar
    no_merge_existing_sidecars = args.no_merge_existing_sidecars
    no_delete_duplicate_sidecar = args.no_delete_duplicate_sidecar

    # Ensure the sidecar extension starts with a dot
    if not sidecar_extension.startswith('.'):
        sidecar_extension = '.' + sidecar_extension

    # Remove the dot from the sidecar extension for jdupes
    sidecar_ext_no_dot = sidecar_extension.lstrip('.')

    # Set up logging based on verbosity level
    verbosity = args.verbose
    if verbosity == 0:
        log_level = logging.WARNING
    elif verbosity == 1:
        log_level = logging.INFO
    elif verbosity >= 2:
        log_level = logging.DEBUG

    logging.basicConfig(format='%(message)s', level=log_level)

    # Clearly state whether in dry run or normal mode
    if dry_run:
        logging.info("Starting in dry run mode.")
        print("Dry run mode: No files will be deleted or modified.")
    else:
        logging.info("Starting in normal mode.")
        print("Normal mode: Files may be deleted and sidecar files created.")

        # Ask for simple yes/no confirmation
        confirm = input("Do you want to proceed? [y/N]: ").strip().lower()
        if confirm not in ('y', 'yes'):
            print("Operation cancelled by user.")
            sys.exit(0)

    # Build the jdupes command
    cmd = [jdupes_path, '--param-order', '--recurse', '--json']
    if hash_db:
        cmd.append(f'--hash-db={hash_db}')

    # Exclude sidecar files unless --no-exclude-sidecar is specified
    if not no_exclude_sidecar:
        cmd.append(f'--ext-filter=noext:{sidecar_ext_no_dot}')

    # Parse the extra commands using shlex to handle quotes and spaces
    if jdupes_extra_cmds:
        try:
            extra_cmds_list = shlex.split(jdupes_extra_cmds)
            # Check for conflicting options
            conflicting_options = {'--ext-filter'}
            if any(opt in extra_cmds_list for opt in conflicting_options):
                logging.warning("Conflicting options detected in --jdupes-extra-cmds. This may override script defaults.")
            cmd.extend(extra_cmds_list)
        except ValueError as e:
            logging.error(f"Error parsing --jdupes-extra-cmds: {e}")
            sys.exit(1)

    cmd += directories
    logging.debug(f"Running command: {' '.join(cmd)}")

    # Run jdupes and capture output
    try:
        if show_progress:
            # Allow stderr to be printed directly for progress display
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=None, text=True)
        else:
            # Capture stderr to handle errors
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except Exception as e:
        logging.error(f"Error running jdupes: {e}")
        sys.exit(1)

    # Wait for the process to complete and read stdout
    jdupes_output, stderr_data = process.communicate()

    # If not showing progress and there is stderr output, log it
    if not show_progress and stderr_data:
        logging.error(f"jdupes error output: {stderr_data}")

    # Check if jdupes ran successfully
    if process.returncode != 0:
        logging.error(f"Error running jdupes.")
        sys.exit(1)

    # Parse the JSON output
    try:
        duplicates = json.loads(jdupes_output)
    except json.JSONDecodeError as e:
        logging.error(f"Error parsing JSON output from jdupes: {e}")
        sys.exit(1)

    # Initialize dry run report content
    report_lines = []

    # Prepare progress bar for processing duplicates
    total_duplicates = len(duplicates.get('matchSets', []))
    if show_progress:
        progress_bar = tqdm(total=total_duplicates, desc='Processing duplicates')
    else:
        progress_bar = None

    if total_duplicates == 0:
        logging.info("No duplicates found.")

    logging.info(f"Processing {total_duplicates} duplicate sets.")

    for dupe_set in duplicates.get('matchSets', []):
        # Extract the list of file paths
        file_list = [file_info['filePath'] for file_info in dupe_set.get('fileList', [])]
        # Get files in directory order
        files_in_order = get_directory_ordered_files(file_list, directories)
        if not files_in_order:
            continue
        # The first file is the one to keep
        keep_file = files_in_order[0]
        delete_files = files_in_order[1:]

        if dry_run:
            # Append actions to the report
            report_lines.append(f"Would keep file: {keep_file}")
            for f in delete_files:
                report_lines.append(f"Would delete duplicate file: {f}")
            sidecar_file = f"{keep_file}{sidecar_extension}"
            sidecar_contents = []

            # Collect the contents that would go into the sidecar file
            for deleted_file in delete_files:
                sidecar_contents.append(deleted_file)
                if not no_merge_existing_sidecars:
                    deleted_sidecar_file = f"{deleted_file}{sidecar_extension}"
                    if os.path.exists(deleted_sidecar_file):
                        report_lines.append(f"Would merge existing sidecar file: {deleted_sidecar_file} into {sidecar_file}")
                        # Read the contents hypothetically
                        try:
                            with open(deleted_sidecar_file, 'r', encoding='utf-8') as dsf:
                                existing_contents = dsf.read().splitlines()
                                sidecar_contents.extend(existing_contents)
                        except Exception as e:
                            report_lines.append(f"Error reading existing sidecar file {deleted_sidecar_file}: {e}")
                        if not no_delete_duplicate_sidecar:
                            report_lines.append(f"Would delete sidecar file: {deleted_sidecar_file}")
                        else:
                            report_lines.append(f"Would not delete sidecar file: {deleted_sidecar_file}")

            report_lines.append(f"Would create sidecar file: {sidecar_file} with contents:")
            for content in sidecar_contents:
                report_lines.append(f"  {content}")
            report_lines.append("")  # Add empty line for readability
        else:
            # Delete the duplicate files
            for f in delete_files:
                try:
                    os.remove(f)
                    logging.info(f"Deleted duplicate file: {f}")
                except FileNotFoundError:
                    logging.error(f"File not found when attempting to delete {f}")
                except PermissionError:
                    logging.error(f"Permission denied when attempting to delete {f}")
                except Exception as e:
                    logging.error(f"Unexpected error deleting file {f}: {e}")

            sidecar_file = f"{keep_file}{sidecar_extension}"
            sidecar_contents = []

            # Collect the contents that will go into the sidecar file
            for deleted_file in delete_files:
                sidecar_contents.append(deleted_file)
                if not no_merge_existing_sidecars:
                    deleted_sidecar_file = f"{deleted_file}{sidecar_extension}"
                    if os.path.exists(deleted_sidecar_file):
                        logging.info(f"Merging existing sidecar file: {deleted_sidecar_file} into {sidecar_file}")
                        # Read the contents
                        try:
                            with open(deleted_sidecar_file, 'r', encoding='utf-8') as dsf:
                                existing_contents = dsf.read().splitlines()
                                sidecar_contents.extend(existing_contents)
                        except Exception as e:
                            logging.error(f"Error reading existing sidecar file {deleted_sidecar_file}: {e}")
                        # Delete the existing sidecar file unless --no-delete-duplicate-sidecar is specified
                        if not no_delete_duplicate_sidecar:
                            try:
                                os.remove(deleted_sidecar_file)
                                logging.info(f"Deleted sidecar file: {deleted_sidecar_file}")
                            except Exception as e:
                                logging.error(f"Error deleting sidecar file {deleted_sidecar_file}: {e}")
                        else:
                            logging.info(f"Retained sidecar file: {deleted_sidecar_file}")

            # Now write or append to the sidecar file for the keep_file
            try:
                # Open the sidecar file in append mode if it exists
                if os.path.exists(sidecar_file):
                    mode = 'a'
                    logging.info(f"Appending to existing sidecar file: {sidecar_file}")
                else:
                    mode = 'w'
                    logging.info(f"Creating new sidecar file: {sidecar_file}")
                with open(sidecar_file, mode, encoding='utf-8') as sf:
                    for content in sidecar_contents:
                        sf.write(f"{content}\n")
            except IOError as e:
                logging.error(f"I/O error creating sidecar file {sidecar_file}: {e}")
            except Exception as e:
                logging.error(f"Unexpected error creating sidecar file {sidecar_file}: {e}")

        if show_progress and progress_bar:
            progress_bar.update(1)

    if show_progress and progress_bar:
        progress_bar.close()

    if dry_run:
        # Write the report to the specified output file
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write('\n'.join(report_lines))
            logging.info(f"Dry run completed. Report written to {output_file}")
        except IOError as e:
            logging.error(f"I/O error writing dry run report to {output_file}: {e}")
        except Exception as e:
            logging.error(f"Unexpected error writing dry run report to {output_file}: {e}")

if __name__ == '__main__':
    main()
