from pathlib import Path
from astropy.io.fits import getheader
from datetime import datetime
import hashlib
import shutil
import re
import logging
import os
import warnings
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from tqdm import tqdm

logging.basicConfig(level=logging.WARNING, filename="sorting_log.log",filemode="w", format="%(asctime)s %(levelname)s %(message)s")

# calibration files
Calib_types = ("dark", "flat", "bias", "lamp")

def is_protected_dir(name: str) -> bool:
    name = name.lower()
    return "calibration" in name or "targets" in name

def get_header_date(file_path: Path):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            header = getheader(file_path)
        for key in ("DATE-OBS", "DATEOBS", "DATE"):
            match = re.search(r"\d{4}-\d{2}-\d{2}", str(header.get(key, "")))
            if match:
                date_name = match.group(0)
                datetime.strptime(date_name, "%Y-%m-%d")
                return date_name
    except Exception:
        return None
    return None

def file_sha256(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

# helper function to extract target directory dynamically
# helper function to extract target directory dynamically
def get_target_dir(file_path: Path, file_type: str) -> Path:
    parts = file_path.parts
    date_idx = -1
    for i, part in enumerate(parts):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", part):
            date_idx = i
            break

    header_date = None
    if file_path.suffix.lower() in (".fits", ".fit"):
        header_date = get_header_date(file_path)

    if date_idx != -1 and date_idx >= 3:
        date_name = header_date or parts[date_idx]

        # Extract observatory, instrument, and mode dynamically from path
        if re.fullmatch(r"\d{4}", parts[date_idx - 1]):
            mode = parts[date_idx - 2]
            inst = parts[date_idx - 3]
            obs = parts[date_idx - 4]
        else:
            mode = parts[date_idx - 1]
            inst = parts[date_idx - 2]
            obs = parts[date_idx - 3]
    else:
        if not header_date:
            return None

        year_idx = next(
            (i for i, part in enumerate(parts) if re.fullmatch(r"\d{4}", part)),
            -1,
        )
        if year_idx < 3:
            return None

        date_name = header_date
        mode = parts[year_idx - 1]
        inst = parts[year_idx - 2]
        obs = parts[year_idx - 3]

    year_name = date_name[:4]

    # Global storage architecture
    fai_base = Path("/observations") / obs / inst / mode

    # 1. Base classification
    if file_type == "meta":
        target = fai_base / "targets" / year_name / date_name / "meta"

    elif file_type == "targets":
        target = fai_base / "targets" / year_name / date_name / "raw"

    elif file_type.startswith("masters/"):
        sub_type = file_type.split("/")[-1]

        target = (fai_base / "calibration" / year_name / date_name / "masters" / sub_type)

    else:
        target = (fai_base / "calibration" / year_name / date_name / file_type)

    # 2. Universal interceptor for any test files overrides the base target
    if "test" in file_path.name.lower():
        target = fai_base / "targets" / year_name / date_name / "test"

    return target

# function to get the required header from "fits" files
def get_imagetype(fits_path: Path) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            header = getheader(fits_path)
        return str(header.get("IMAGETYP", "")).lower()
    except Exception as e:
        logging.warning(f"[Error] Corrupted FITS left in place {fits_path}: {e}")
        return None

def get_masters_readoutm(fits_path: Path) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            header = getheader(fits_path)
        readout = str(header.get("READOUTM", "")).lower()

        return (
            "preflash" in readout or
            "RBI flood" in readout or
            "pre-flash" in readout
        )

    except Exception as e:
        logging.debug(f"[Error] Failed to read READOUT from {fits_path}: {e}")
        return False

# function to determine the type of "fits" file: calibration and targets
def classify_file(fits_path: Path) -> str:
    imagetyp = get_imagetype(fits_path)

    if imagetyp is None:
        return None

    for t in Calib_types:
        if t in imagetyp:
            # determining for master calibration
            if get_masters_readoutm(fits_path):
                return f"masters/{t}"

            return t

    return "targets"

# function to move non-"fits" files to a separate meta folder
def move_non_fits_to_meta(folder: Path, meta_dir: Path):

    if not folder.exists():
        return

    try:
        # Overriding local meta_dir with universal dynamic path
        dynamic_meta_dir = get_target_dir(folder, "meta")
        if dynamic_meta_dir:
            meta_dir = dynamic_meta_dir

        if meta_dir is None:
            logging.warning(f"[Error] Could not build meta path for: {folder}")
            return

        meta_dir.mkdir(parents=True, exist_ok=True)
        end_d = meta_dir / folder.name

        # if the file already exists
        if end_d.exists():
            if (folder.stat().st_size == end_d.stat().st_size and
                    file_sha256(folder) == file_sha256(end_d)):
                folder.unlink()
            else:
                logging.warning(
                    f"[Error] Same meta name but different content; source left in place: "
                    f"{folder} -> {end_d}"
                )
            return

        shutil.move(str(folder), str(end_d))

        #logging.info(f"meta: {item} -> {end_d}")

    except PermissionError:
        logging.warning(
            f"File is locked or no access rights: {folder}"
        )

    except OSError as e:
        logging.warning(
            f"Access error when moving {folder}: {e}"
        )

    except Exception as e:
        logging.error(
            f"Unexpected error when processing {folder}: {e}"
        )

# | Part Two |

# Pre-loading files into lists
def scan_files(root_dir):
    fits_files = []
    meta_files = []

    for dirpath, dirnames, filenames in os.walk(root_dir):

        dirnames[:] = [d for d in dirnames if not is_protected_dir(d)]

        dirpath = Path(dirpath)

        for filename in filenames:

            file_path = dirpath / filename

            if file_path.suffix.lower() in (".fits", ".fit"):
                fits_files.append(file_path)

            else:
                meta_files.append(file_path)

    return fits_files, meta_files

# function to safely remove directory tree if it is completely empty
def cleanup_empty_dirs(directory: Path):
    if not directory.exists():
        return
    directories = []
    for root, dirs, _ in os.walk(directory, topdown=True):
        dirs[:] = [d for d in dirs if not is_protected_dir(d)]
        directories.extend(Path(root) / d for d in dirs)
    for dir_path in reversed(directories):
        if dir_path.exists() and not any(dir_path.iterdir()):
            try:
                dir_path.rmdir()
            except OSError:
                pass
    if not any(directory.iterdir()):
        try:
            directory.rmdir()
        except OSError:
            pass

# function to move targets and calibration files to corresponding folders
def process_directory(fits_path: Path):

    try:
        file_type = classify_file(fits_path)

        if file_type is None:
            return

        target_dir = get_target_dir(fits_path, file_type)

        if not target_dir:
            logging.error(f"[Error] Could not build target path for: {fits_path}")
            return

        target_dir.mkdir(parents=True, exist_ok=True)

        target_path = target_dir / fits_path.name

        if target_path.exists():
            try:
                if (fits_path.stat().st_size == target_path.stat().st_size and
                        file_sha256(fits_path) == file_sha256(target_path)):
                    fits_path.unlink()
                else:
                    logging.warning(
                        f"[Error] Same name but different content; source left in place: "
                        f"{fits_path} -> {target_path}"
                    )
            except OSError as e:
                logging.warning(f"[Error] Hash check failed for {fits_path}: {e}")
            return

        shutil.move(str(fits_path), str(target_path))

    except Exception as e:
        logging.error(f"[Error] moving error for {fits_path}: {e}")

    #move_non_fits_to_meta(Base_dir)

# functions to determine year and date by folders
def is_year_folder(name: str):
    return re.fullmatch(r"\d{4}", name) is not None

def is_date_folder(name: str):
    return len(name) == 10 and name[4] == "-" and name[7] == "-"

# function to create sorting folders in the directory
def process_telescope(telescope_path: Path):

    # creating "observ_mode" folder in "...\Observation\observatory_id\instrument_id" directory
    # creating calibration and targets folders in the observ_mode folder
    observ_mode_dir = telescope_path / "observ_mode"
    calib_out = observ_mode_dir / "calibration"
    targets_out = observ_mode_dir / "targets"

    calib_out.mkdir(parents=True, exist_ok=True)
    targets_out.mkdir(parents=True, exist_ok=True)

    # search for observation mode
    for mode_dir in telescope_path.iterdir():

        if not mode_dir.is_dir() or mode_dir.name == "observ_mode":
            continue

        # determining the year
        for year_dir in telescope_path.rglob("*"):

            if not year_dir.is_dir():
                continue

            if not is_year_folder(year_dir.name):
                continue

            year_name = year_dir.name

            # determining the date
            for date_dir in year_dir.iterdir():

                if not date_dir.is_dir() or not is_date_folder(date_dir.name):
                    continue

                date_name = date_dir.name

                # Moving Calibration folders
                calib_src = date_dir / "calib"

                if calib_src.exists():

                    calib_target = (calib_out /year_name / date_name)
                    calib_target.mkdir(parents=True, exist_ok=True)

                    for sub in ["flat", "bias", "dark", "lamp"]:
                        src = calib_src / sub
                        if src.exists():
                            end_d = calib_target / sub
                            if not end_d.exists():
                                shutil.move(str(src), str(end_d))

                # Moving targets folders
                targets_src = date_dir / "targets"

                if targets_src.exists():

                    targets_target = (targets_out /year_name /date_name)
                    targets_target.mkdir(parents=True, exist_ok=True)
                    end_d = targets_target / "targets"

                    if not end_d.exists():
                        shutil.move(str(targets_src), str(end_d))

                # moving meta
                meta_target = (targets_out /year_name /date_name /"meta")
                meta_target.mkdir(parents=True, exist_ok=True)

                # meta from calib
                calib_meta = date_dir / "meta"

                if calib_meta.exists():
                    for file in calib_meta.iterdir():
                        end_d = meta_target / file.name
                        if not end_d.exists():
                            shutil.move(str(file), str(end_d))

                # meta from targets
                targets_meta = date_dir / "meta"

                if targets_meta.exists():
                    for file in targets_meta.iterdir():
                        end_d = meta_target / file.name
                        if not end_d.exists():
                            shutil.move(str(file), str(end_d))

# |Execution block|

if __name__ == "__main__":
    # 1. Specifying the path to the telescope archive
    base = Path(r"/observations/tshao/zeiss1000_east/ccd")
    
    # 2. Scanning all files
    fits_files, meta_files = scan_files(base)
    
    # 3. Creating a single queue for all tasks
    # We combine fits and meta into one list of (file_path, type)
    tasks = [(f, "fits") for f in fits_files] + [(m, "meta") for m in meta_files]
    
    # 4. Processing everything with a single ThreadPool
    with ThreadPoolExecutor(max_workers=8) as executor:
        # Define a single runner function
        def run_task(task_item):
            file_path, task_type = task_item
            if task_type == "fits":
                return process_directory(file_path)
            else:
                return move_non_fits_to_meta(file_path, None)
        
        # Execute and show a single progress bar
        list(tqdm(executor.map(run_task, tasks), total=len(tasks), desc="Processing files"))

    # 5. Final cleanup
    cleanup_empty_dirs(base)

    # =========================================================================
    # WARNING: EVERYTHING BELOW IS COMMENTED OUT (with a # before the lines)
    # so the script doesn't traverse the whole telescope archive prematurely!
    # =========================================================================
    
    # base_dir = Path(r"/observations/tshao/zeiss1000_east/ccd")
    #
    # for obs_dir in base_dir.iterdir():
    #     if not obs_dir.is_dir():
    #         continue
    #     for telescope_dir in obs_dir.iterdir():
    #         if telescope_dir.is_dir():
    #             process_telescope(telescope_dir)
