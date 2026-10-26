# mapillary_keys_ids

The "Mapillary Traffic Sign Dataset" contains image keys that are no longer
useful. [mapillary_image_key_image_id.csv](mapillary_image_key_image_id.csv)
maps the old `image_key` to the new `image_id`. Of the original 41,909 image
keys, 885 could not be resolved.

The Python tools optionally enrich these IDs with coordinates, altitude, and
capture dates through the Mapillary Graph API. The original mapping CSV is
left unchanged.

## Run locally

Requires Python 3.10+. Install the two dependencies, `requests` and
`python-dotenv`, from the repository directory:

```powershell
python -m pip install -r requirements.txt
```

Prepare the output files without a token or any API requests:

```powershell
python process_csv.py --prepare-only --output output/mapillary_metadata.csv
```

This reads the mapping CSV at the repository root. The ignored `output/`
directory receives a metadata CSV, a status CSV, and a SQLite checkpoint.
All valid IDs initially have `pending` status; unresolved IDs are `skipped`.

To fetch metadata, copy `.env.example` to `.env` beside `utils.py` and set your
Mapillary client or user access token:

```dotenv
MAPILLARY_ACCESS_TOKEN=your_mapillary_access_token
```

The `.env` file is excluded from Git. You can also set this environment
variable directly. Live lookups require internet access to
`https://graph.mapillary.com` and a valid token with access to the requested
images. The tools send the token in the authorization header, not the URL.

Fetch the next five unprocessed IDs:

```powershell
python process_csv.py --limit 5 --output output/mapillary_metadata.csv
```

Rerun the same command to continue. Remove `--limit 5` to process all remaining
IDs. Other examples:

```powershell
# Resume and also retry previously failed IDs.
python process_csv.py --retry-errors --output output/mapillary_metadata.csv

# Use another mapping CSV and at most one request start per second.
python process_csv.py mapillary_image_key_image_id.csv --output output/other_metadata.csv --rate 1

# Use the default input and output beside the original mapping CSV.
python process_csv.py
```

The final command writes `mapillary_image_key_image_id_metadata.csv`, its
`_status.csv`, and a `.sqlite3` checkpoint at the repository root. Generated
metadata files, checkpoints, locks, and logs are excluded from Git.

## Output and recovery

The metadata CSV has exactly these columns:

```text
image_key,image_id,lat,lon,alt,capture_date
```

- Every input row retains its original key and order. Duplicate numeric IDs
  are queried once.
- Positive integer IDs and whole-number decimal IDs are normalized without
  float conversion: `172605854746792.0` becomes `172605854746792`.
- Empty, fractional, and nonnumeric IDs receive blank API fields. An old
  `image_key` is never used as a fallback ID.
- Coordinates come from the API's original image `geometry`. `alt` is altitude
  in meters and is blank when unavailable. `capture_date` is an ISO 8601 UTC
  timestamp converted from the API's `captured_at` Unix milliseconds.

For `output/mapillary_metadata.csv`, the companion files are:

| File | Purpose |
| --- | --- |
| `mapillary_metadata_status.csv` | One status and reason per input row: `ok`, `skipped`, `error`, or `pending`. |
| `mapillary_metadata.sqlite3` | Durable results used to resume without repeating successful lookups. |
| `mapillary_metadata.lock` | Prevents simultaneous command-line runs for the same output. |

Blank metadata can mean skipped, failed, or pending; check the status CSV.
Keep the SQLite checkpoint with its output. Results are committed after every
lookup, and full CSV snapshots are refreshed after the first lookup, every
100 lookups, and on normal exit or Ctrl+C. A lock file remaining after exit is
normal; the operating system releases the lock automatically.

Requests share an HTTP session and start at most twice per second by default.
Temporary errors receive up to five total attempts, respecting `Retry-After`.
Authentication failures, sustained temporary failures, or ten consecutive image
errors stop the run while retaining completed results. Rerun to resume, adding
`--retry-errors` when previously failed image lookups should be retried.

## Python API

```python
from utils import get_image_metadata

image_id = input("Mapillary image ID: ").strip()
metadata = get_image_metadata(image_id)
print(metadata)
```

`get_image_metadata(image_id, access_token=None, timeout=30)` returns
`image_id`, `lat`, `lon`, `altitude`, `capture_date`, and `captured_at`.
`set_image_request(...)` builds request arguments without contacting the API;
`connect_to_mapillary(...)` checks the HTTP status and returns the response.
All three require a positive integer or numeric-string ID.

An explicit token takes priority over the environment, which takes priority
over `.env`. The `.env` file is located relative to `utils.py`, independently
of the current working directory. Invalid inputs or malformed metadata raise
`ValueError`; HTTP, connection, and timeout errors raise `requests` exceptions.
Missing altitude returns `None`.

## Project structure and checks

```text
mapillary_image_key_image_id.csv   Original key-to-ID mapping
process_csv.py                     CSV processing, retries, checkpoints, CLI
utils.py                           Mapillary request and metadata helpers
requirements.txt                   Python dependencies
.env.example                       Empty access-token template
tests/                             Offline regression tests
```

Run the tests without making live API requests:

```powershell
python -m unittest discover -s tests -v
```

## Attribution and license

This extends the mapping published in
[viajerus/mapillary_keys_ids](https://github.com/viajerus/mapillary_keys_ids).
The request uses Mapillary's public Graph API, as illustrated in
[Mapillary's API examples](https://github.com/mapillary/api-demo).
This project is not affiliated with Mapillary.

The upstream repository does not include a license file, and this contribution
does not add one. No additional reuse permissions are granted by this README.
