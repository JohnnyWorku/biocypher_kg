import yaml
import requests
import typer
import logging
from pathlib import Path
from tqdm import tqdm
import io
import math
import time
import gzip
import zipfile
import shutil
from urllib.parse import urlparse, urljoin
from html.parser import HTMLParser

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = typer.Typer()


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

def download_with_retry(url, output_path, max_retries=5):
    """Download a file with retry logic and a progress bar.

    Treats a 0-byte result as a failure and retries — this handles S3/FTP
    redirects that return HTTP 200 with an empty body on the first request.
    The incomplete file is deleted before each retry so it cannot be mistaken
    for a completed download on the next run.
    """
    for attempt in range(max_retries):
        try:
            response = requests.get(url, stream=True)
            response.raise_for_status()
            total_size = int(response.headers.get('content-length', 0))

            with open(output_path, 'wb') as f, tqdm(
                desc=output_path.name,
                total=total_size,
                unit='B',
                unit_scale=True,
                unit_divisor=1024,
            ) as bar:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
                    bar.update(len(chunk))

            # Validate: a 0-byte file means the server returned an empty body.
            # This happens with some S3/FTP endpoints that redirect on first hit.
            written = output_path.stat().st_size if output_path.exists() else 0
            if written == 0:
                output_path.unlink(missing_ok=True)
                raise ValueError("Downloaded file is 0 bytes — likely an S3/FTP redirect or transient error")

            return True

        except (requests.RequestException, ValueError) as e:
            logger.warning(f"Attempt {attempt + 1}/{max_retries} failed for {url}: {e}")
            # Remove any partial/empty file so it won't fool already_downloaded()
            if output_path.exists():
                output_path.unlink(missing_ok=True)
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.info(f"Retrying in {wait}s...")
                time.sleep(wait)

    logger.error(f"All {max_retries} attempts failed for {url}")
    return False


def already_downloaded(output_path, url):
    """Return True if a local file exists, is non-empty, and appears to match
    the remote file size.

    A 0-byte local file is NEVER considered complete — this guards against
    S3/FTP endpoints that return HTTP 200 with an empty body on the first
    request, leaving behind a 0-byte file that would otherwise be skipped
    silently on every subsequent run.

    Size-check logic:
      - If the HEAD request succeeds and the server returns a non-zero
        content-length, compare it against the local file size.
      - If the server returns content-length: 0 (common with S3 presigned
        redirects) or the HEAD request fails entirely, trust the local file
        as long as it is non-empty.
    """
    local_size = output_path.stat().st_size if output_path.exists() else 0
    if local_size == 0:
        # Always re-attempt — 0-byte files are never valid downloads.
        if output_path.exists():
            logger.warning(f"Found 0-byte file {output_path.name} — will re-download")
            output_path.unlink(missing_ok=True)
        return False
    try:
        response = requests.head(url, timeout=10)
        remote_size = int(response.headers.get('content-length', 0))
        if remote_size == 0:
            # Server didn't provide content-length (or S3 redirect) — trust local file
            return True
        return local_size == remote_size
    except requests.RequestException:
        # HEAD request failed — trust the non-empty local file
        return True


def extract_compressed(file_path):
    """Decompress a .gz or .zip file in place."""
    if file_path.suffix == '.gz':
        extracted_path = file_path.with_suffix('')
        with gzip.open(file_path, 'rb') as f_in, open(extracted_path, 'wb') as f_out:
            shutil.copyfileobj(f_in, f_out)
        file_path.unlink()
        logger.info(f"Extracted {file_path} to {extracted_path}")
    elif file_path.suffix == '.zip':
        extract_dir = file_path.parent
        with zipfile.ZipFile(file_path, 'r') as zf:
            zf.extractall(extract_dir)
        file_path.unlink()
        logger.info(f"Extracted {file_path} to {extract_dir}")


def compress_gzip(file_path):
    """Gzip a file in place, appending .gz to its name."""
    compressed_path = file_path.with_suffix(file_path.suffix + '.gz')
    with open(file_path, 'rb') as f_in, gzip.open(compressed_path, 'wb') as f_out:
        shutil.copyfileobj(f_in, f_out)
    file_path.unlink()
    logger.info(f"Compressed {file_path} to {compressed_path}")


def parse_url_comment(url_str):
    """Split 'url # comment' into (url, comment). Comment may be None."""
    if '#' in url_str:
        url, comment = url_str.split('#', 1)
        return url.strip(), comment.strip()
    return url_str.strip(), None


def should_skip_extract(comment):
    """Return True if the inline URL comment requests keeping the file compressed."""
    return bool(comment and (
        'no extract' in comment.lower() or 'keep gzipped' in comment.lower()
    ))


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------

DEFAULT_SAMPLE_FRACTION = 0.01  # 1% by default


def _open_text(file_path):
    """Open a file for text reading, transparently decompressing .gz."""
    if file_path.suffix == '.gz':
        return gzip.open(file_path, 'rt', errors='replace')
    return open(file_path, 'r', errors='replace')


def create_sample(file_path, output_dir, sample_root, fraction=DEFAULT_SAMPLE_FRACTION):
    """Write a line-based sample of file_path to the mirrored path under sample_root.

    The sample has the exact same filename and compression as the original:
      - .gz  -> sample is also gzipped
      - plain text -> sample is plain text

    If the file is small enough that the sample would cover the whole file
    (i.e. total_lines <= 1/fraction), the entire file is copied as the sample.

    Returns True if a new sample was created, False if it already existed or
    the file could not be read.

    Args:
        file_path:   Path to the fully post-processed file.
        output_dir:  Root output directory (used to compute relative path).
        sample_root: Root directory for samples (output_dir / 'sample').
        fraction:    Fraction of lines to sample (default 1%).
    """
    try:
        rel_path = file_path.relative_to(output_dir)
    except ValueError:
        logger.warning(f"Cannot compute relative path for sample: {file_path}")
        return False

    sample_path = sample_root / rel_path
    if sample_path.exists() and sample_path.stat().st_size > 0:
        return False  # already exists — caller decides whether to log

    sample_path.parent.mkdir(parents=True, exist_ok=True)

    # Count lines
    try:
        total_lines = sum(1 for _ in _open_text(file_path))
    except Exception as e:
        logger.warning(f"Skipping sample for {file_path.name}: cannot read as text ({e})")
        return False

    if total_lines == 0:
        logger.warning(f"Skipping sample for {file_path.name}: file is empty")
        return False

    # If the file is small enough that sampling would cover it entirely, copy the whole file
    min_lines_for_sampling = math.ceil(1 / fraction) if fraction > 0 else float('inf')
    if total_lines <= min_lines_for_sampling:
        shutil.copy2(str(file_path), str(sample_path))
        return True

    n_sample = max(1, math.ceil(total_lines * fraction))

    try:
        buf = io.StringIO()
        with _open_text(file_path) as f_in:
            for i, line in enumerate(f_in):
                if i >= n_sample:
                    break
                buf.write(line)

        sample_bytes = buf.getvalue().encode()

        if file_path.suffix == '.gz':
            with gzip.open(sample_path, 'wb') as f_out:
                f_out.write(sample_bytes)
        else:
            with open(sample_path, 'wb') as f_out:
                f_out.write(sample_bytes)

        return True

    except Exception as e:
        logger.warning(f"Failed to create sample for {file_path.name}: {e}")
        return False


# ---------------------------------------------------------------------------
# Directory scraping
# ---------------------------------------------------------------------------

class _LinkParser(HTMLParser):
    """Minimal HTML parser that collects href values from anchor tags."""
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            for name, value in attrs:
                if name == 'href' and value:
                    self.links.append(value)


def scrape_directory(dir_url, output_dir, max_retries=3):
    """Fetch an HTML directory listing and download every file linked from it.
    Subdirectory links (trailing slash) are skipped.
    Files are always kept as-is — no decompression.
    Returns (downloaded, skipped, failed_filenames) where failed_filenames is a list of filenames."""
    for attempt in range(max_retries):
        try:
            response = requests.get(dir_url, timeout=30)
            response.raise_for_status()
            break
        except requests.RequestException as e:
            logger.warning(f"Attempt {attempt + 1} failed fetching directory {dir_url}: {e}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
    else:
        logger.error(f"Could not fetch directory listing: {dir_url}")
        return 0, 0, [dir_url]

    parser = _LinkParser()
    parser.feed(response.text)

    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded = skipped = 0
    failed_filenames = []

    for href in parser.links:
        # Skip parent dir links, anchors, absolute paths, and subdirectories
        if not href or href.startswith(('?', '#', '/')) or href in ('..', './') or href.endswith('/'):
            continue

        file_url = urljoin(dir_url, href)
        filename = Path(urlparse(file_url).path).name
        if not filename:
            continue

        output_path = output_dir / filename

        if already_downloaded(output_path, file_url):
            logger.info(f"Skipped {file_url} (already exists)")
            skipped += 1
            continue

        if download_with_retry(file_url, output_path):
            logger.info(f"Downloaded {file_url}")
            downloaded += 1
        else:
            logger.error(f"Failed to download {file_url}")
            failed_filenames.append(filename)

    return downloaded, skipped, failed_filenames


# ---------------------------------------------------------------------------
# URL processing
# ---------------------------------------------------------------------------

def is_filename_url_dict(d):
    """Return True if a dict uses {filename: url} pairs instead of {sub_key: url_list}."""
    return isinstance(d, dict) and all(
        isinstance(v, str) and v.startswith('http')
        for v in d.values()
    )


def _post_processed_path(output_path, extract, compress, comment, move_to_dest):
    """Return the path where the file will end up after all post-processing.
    Used to skip re-downloading files that were already processed in a previous run."""
    p = output_path
    # After extraction: .gz/.zip -> decompressed (only if file is actually compressed)
    if extract and not should_skip_extract(comment) and p.suffix in ('.gz', '.zip'):
        if p.suffix == '.gz':
            p = p.with_suffix('')
    # After compression: plain file -> .gz appended
    elif compress == 'gzip' and p.suffix != '.gz':
        p = p.with_suffix(p.suffix + '.gz')
    # After move_to: file lands in a different directory
    if move_to_dest:
        p = move_to_dest / p.name
    return p


def download_one(url, output_path, extract, compress=None, move_to_dest=None,
                 output_dir=None, sample_root=None, sample_fraction=DEFAULT_SAMPLE_FRACTION):
    """Download a single URL to output_path, applying post-processing and optional move.

    Post-processing order:
      1. extract (decompress .gz / .zip) or compress (gzip plain file)
      2. move_to_dest: if provided, move the final file to this resolved Path
      3. create_sample: if sample_root provided, write a reduced sample copy

    compress: None (no compression) or 'gzip' (gzip after download).
    move_to_dest: resolved Path to move the file into after post-processing, or None.
    output_dir/sample_root: if both provided, a line sample is created after download.
    Returns (downloaded, skipped, failed_filenames, sample_created) where
      failed_filenames is a list of filenames and sample_created is a bool."""
    url, comment = parse_url_comment(url)

    # A directory at the output path means a previous run misidentified the
    # filename as a sub_key and called mkdir on it — remove it so we can write the file.
    if output_path.is_dir():
        logger.warning(f"Removing unexpected directory at {output_path} (will be replaced by file)")
        shutil.rmtree(output_path)

    # Check the post-processed path first: if a previous run already extracted,
    # compressed, or moved the file, the original filename won't exist on disk.
    post_processed_path = _post_processed_path(output_path, extract, compress, comment, move_to_dest)
    if already_downloaded(post_processed_path, url):
        logger.info(f"Skipped {url} (already exists as {post_processed_path.name})")
        return 0, 1, [], False

    if already_downloaded(output_path, url):
        logger.info(f"Skipped {url} (already exists)")
        return 0, 1, [], False

    filename = output_path.name

    if not download_with_retry(url, output_path):
        logger.error(f"Failed to download {url}")
        return 0, 0, [filename], False

    # --- post-processing: extract or compress ---
    if extract and not should_skip_extract(comment) and output_path.suffix in ('.gz', '.zip'):
        extract_compressed(output_path)
        if output_path.suffix == '.gz':
            output_path = output_path.with_suffix('')
    elif compress == 'gzip' and output_path.suffix != '.gz':
        compress_gzip(output_path)
        output_path = output_path.with_suffix(output_path.suffix + '.gz')

    # --- post-processing: move_to ---
    if move_to_dest:
        move_to_dest.mkdir(parents=True, exist_ok=True)
        dest_path = move_to_dest / output_path.name
        source_dir = output_path.parent
        shutil.move(str(output_path), str(dest_path))
        output_path = dest_path
        logger.info(f"Moved {output_path.name} to {dest_path}")
        if source_dir.exists() and not any(source_dir.iterdir()):
            source_dir.rmdir()
            logger.info(f"Removed empty directory: {source_dir}")

    # --- post-processing: sample ---
    sample_created = False
    if output_dir and sample_root:
        if output_path.exists():
            if move_to_dest:
                moved_sample_root = move_to_dest / 'sample'
                sample_created = create_sample(output_path, move_to_dest, moved_sample_root, sample_fraction)
            else:
                sample_created = create_sample(output_path, output_dir, sample_root, sample_fraction)
        else:
            logger.warning(f"Post-processed file not found for sampling: {output_path}")

    logger.info(f"Downloaded {url}")
    return 1, 0, [], sample_created


def resolve_move_to(dest_str, output_dir):
    """Resolve a move_to destination relative to output_dir."""
    return (output_dir / dest_str).resolve()


def process_urls(urls, output_dir, source_key, sub_key=None, extract=True, compress=None,
                 move_to=None, sample_root=None, sample_fraction=DEFAULT_SAMPLE_FRACTION):
    """Recursively process URLs from a YAML source entry.

    Returns (downloaded, skipped, failed_filenames, samples_created).
    """
    downloaded = skipped = samples_created = 0
    failed_filenames = []
    sub_dir = output_dir / source_key / sub_key if sub_key else output_dir / source_key
    move_to = move_to or {}

    def get_move_to_dest(filename):
        dest_str = move_to.get(filename)
        return resolve_move_to(dest_str, output_dir) if dest_str else None

    def dl(url, path, filename):
        return download_one(url, path, extract, compress, get_move_to_dest(filename),
                            output_dir, sample_root, sample_fraction)

    if isinstance(urls, str):
        sub_dir.mkdir(parents=True, exist_ok=True)
        filename = Path(urlparse(urls.split('#')[0].strip()).path).name
        d, s, f, sc = dl(urls, sub_dir / filename, filename)
        return d, s, f, sc

    if isinstance(urls, dict):
        if is_filename_url_dict(urls):
            for filename, url in urls.items():
                sub_dir.mkdir(parents=True, exist_ok=True)
                d, s, f, sc = dl(url, sub_dir / filename, filename)
                downloaded += d; skipped += s; failed_filenames += f; samples_created += sc
        else:
            for sub, sub_urls in urls.items():
                d, s, f, sc = process_urls(sub_urls, output_dir, source_key, sub,
                                           extract=extract, compress=compress, move_to=move_to,
                                           sample_root=sample_root, sample_fraction=sample_fraction)
                downloaded += d; skipped += s; failed_filenames += f; samples_created += sc
        return downloaded, skipped, failed_filenames, samples_created

    # List of URLs
    for url in urls:
        sub_dir.mkdir(parents=True, exist_ok=True)
        url_clean = url.split('#')[0].strip()
        filename = Path(urlparse(url_clean).path).name
        d, s, f, sc = dl(url, sub_dir / filename, filename)
        downloaded += d; skipped += s; failed_filenames += f; samples_created += sc

    return downloaded, skipped, failed_filenames, samples_created


# ---------------------------------------------------------------------------
# zip_extract processing
# ---------------------------------------------------------------------------

def process_zip_extract(zip_extract_list, output_dir, source_key, compress=None,
                        sample_root=None, sample_fraction=DEFAULT_SAMPLE_FRACTION):
    """Download a zip, extract only the specified files from it, then delete the zip.

    Returns (downloaded, skipped, failed_filenames, samples_created).
    """
    downloaded, skipped, samples_created = 0, 0, 0
    failed_filenames = []
    sub_dir = output_dir / source_key
    sub_dir.mkdir(parents=True, exist_ok=True)

    for entry in zip_extract_list:
        zip_url = entry.get('url')
        target_files = entry.get('files', [])

        if not zip_url:
            logger.warning("zip_extract entry missing 'url' — skipping")
            continue

        # Check if all target files already exist (post-processed)
        all_exist = all(
            (sub_dir / (f + '.gz' if compress == 'gzip' else f)).exists()
            for f in target_files
        )
        if all_exist:
            logger.info(f"Skipped {zip_url} (all target files already exist)")
            skipped += len(target_files)
            continue

        zip_filename = Path(urlparse(zip_url).path).name
        zip_path = sub_dir / zip_filename

        if not download_with_retry(zip_url, zip_path):
            logger.error(f"Failed to download {zip_url}")
            failed_filenames.append(zip_filename)
            continue

        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                available = zf.namelist()
                for target in target_files:
                    match = next((n for n in available if n.endswith(target)), None)
                    if not match:
                        logger.warning(f"File '{target}' not found in {zip_filename}. Available: {available}")
                        failed_filenames.append(target)
                        continue
                    extracted_path = sub_dir / target
                    with zf.open(match) as src, open(extracted_path, 'wb') as dst:
                        shutil.copyfileobj(src, dst)
                    logger.info(f"Extracted {target} from {zip_filename}")

                    if compress == 'gzip':
                        compress_gzip(extracted_path)
                        extracted_path = extracted_path.with_suffix(extracted_path.suffix + '.gz')

                    if sample_root:
                        sc = create_sample(extracted_path, output_dir, sample_root, sample_fraction)
                        samples_created += sc

                    downloaded += 1
        except zipfile.BadZipFile as e:
            logger.error(f"Bad zip file {zip_filename}: {e}")
            failed_filenames.append(zip_filename)
        finally:
            zip_path.unlink(missing_ok=True)

    return downloaded, skipped, failed_filenames, samples_created


# ---------------------------------------------------------------------------
# Sample backfill
# ---------------------------------------------------------------------------

def ensure_samples(output_dir, sample_root, source_key=None,
                   sample_fraction=DEFAULT_SAMPLE_FRACTION):
    """Create missing samples for files already present in output_dir.

    If source_key is given, only walks output_dir/source_key/.
    Returns the number of new samples created.
    """
    if not sample_root:
        return 0

    walk_root = output_dir / source_key if source_key else output_dir
    if not walk_root.exists():
        return 0

    created = 0
    for file_path in sorted(walk_root.rglob('*')):
        if not file_path.is_file():
            continue
        # Skip anything inside the sample tree
        try:
            file_path.relative_to(sample_root)
            continue
        except ValueError:
            pass

        sc = create_sample(file_path, output_dir, sample_root, sample_fraction)
        created += sc

    return created


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

@app.command()
def download_data(
    config_file: str = typer.Option(..., "--config-file", help="Path to the species data source config YAML"),
    output_dir: str = typer.Option("./data", "--output-dir", help="Output directory for downloads"),
    sample_fraction: float = typer.Option(DEFAULT_SAMPLE_FRACTION, "--sample-fraction",
                                          help="Fraction of lines to include in sample files (0 to disable)"),
):
    """Download data from sources defined in the given species config YAML.

    Examples:
        python download_data.py --config-file config/hsa/hsa_data_source_config.yaml
        python download_data.py --config-file config/dmel/dmel_data_source_config.yaml
        python download_data.py --config-file config/mmu/mmu_data_source_config.yaml
    """
    config_path = Path(config_file)
    if not config_path.exists():
        logger.error(f"Config file not found: {config_path}")
        raise typer.Exit(1)

    logger.info(f"Loading config from {config_path}...")
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)

    if not config:
        logger.error("Invalid config: empty or malformed")
        raise typer.Exit(1)

    logger.info(f"Downloading data to {output_dir}...")
    output_dir = Path(output_dir)
    sample_root = output_dir / 'sample' if sample_fraction > 0 else None
    total_downloaded = total_skipped = 0

    # Structured failure report: {source_key: [filename, ...]}
    failed_by_source: dict[str, list[str]] = {}

    for source_key, source_data in config.items():
        if not isinstance(source_data, dict):
            logger.warning(f"Unexpected format for source '{source_key}' — skipping")
            continue

        extract = source_data.get('extract', True)
        compress = source_data.get('compress', None)
        move_to = source_data.get('move_to', {})
        has_handled = False
        key_samples = 0
        key_failed: list[str] = []

        # Handle 'url' key
        if 'url' in source_data:
            d, s, f, sc = process_urls(source_data['url'], output_dir, source_key, extract=extract,
                                       compress=compress, move_to=move_to,
                                       sample_root=sample_root, sample_fraction=sample_fraction)
            total_downloaded += d; total_skipped += s; key_failed += f; key_samples += sc
            has_handled = True

        # Handle 'directories' key — scrape each HTML directory listing
        if 'directories' in source_data:
            for dir_name, dir_url in source_data['directories'].items():
                dir_output = output_dir / source_key / dir_name
                logger.info(f"Scraping directory {dir_url} to {dir_output}...")
                d, s, f = scrape_directory(dir_url, dir_output)
                total_downloaded += d; total_skipped += s; key_failed += f
            has_handled = True

        # Handle 'zip_extract' key — download zip and extract specific files
        if 'zip_extract' in source_data:
            d, s, f, sc = process_zip_extract(source_data['zip_extract'], output_dir, source_key,
                                              compress=compress, sample_root=sample_root,
                                              sample_fraction=sample_fraction)
            total_downloaded += d; total_skipped += s; key_failed += f; key_samples += sc
            has_handled = True

        if not has_handled:
            logger.warning(f"No 'url', 'directories', or 'zip_extract' key found for '{source_key}' — skipping")
            continue

        # Backfill samples for files that were skipped (already downloaded) this run
        if sample_root:
            key_samples += ensure_samples(output_dir, sample_root, source_key, sample_fraction)

        if key_samples:
            logger.info(f"Samples created for {source_key}")

        if key_failed:
            failed_by_source[source_key] = key_failed

    # ---------------------------------------------------------------------------
    # Final summary
    # ---------------------------------------------------------------------------
    n_failed_files = sum(len(v) for v in failed_by_source.values())
    summary = (
        f"Download complete: {total_downloaded} downloaded, "
        f"{total_skipped} skipped, {n_failed_files} failed"
    )

    if failed_by_source:
        logger.error(summary)
        lines = ["\nError report:"]
        for source_key, filenames in failed_by_source.items():
            lines.append(f"{source_key}:")
            for name in filenames:
                lines.append(f"        {name}")
        logger.error("\n".join(lines))
    else:
        logger.info(summary)


if __name__ == "__main__":
    app()

