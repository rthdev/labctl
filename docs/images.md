# Images and Provenance

## Bundled managed images

The bundled catalog pins these x86_64 QCOW2 releases:

| Image ID | Pinned build | Published checksum source |
| --- | --- | --- |
| `rocky:9` | `Rocky-9-GenericCloud-Base-9.8-20260525.0.x86_64` | The matching per-image `qcow2.CHECKSUM` file on `dl.rockylinux.org` |
| `rocky:10` | `Rocky-10-GenericCloud-Base-10.2-20260525.0.x86_64` | The matching per-image `qcow2.CHECKSUM` file on `dl.rockylinux.org` |
| `fedora:44` | `Fedora-Server-Guest-Generic-44-1.7.x86_64` | `Fedora-Server-44-1.7-x86_64-CHECKSUM` |

Each catalog entry contains an exact, non-`latest` HTTPS image URL and its
published SHA-256 digest. Pull and inspect an image with:

    labctl image pull rocky:9
    labctl image inspect rocky:9
    labctl image list --cached

The Rocky checksum files and detached signatures were verified with the Rocky
9 and Rocky 10 release keys. The Fedora clear-signed checksum file was verified
with the Fedora 44 primary key. `checksum_source_url` records the exact source
used for each bundled digest.

Interactive `image pull` displays a byte-based progress bar when the server
provides a positive Content-Length. If the total is unavailable or invalid, it
reports bytes transferred and `total unknown`, without inventing a percentage.
Progress is cleared on success, failure, and interruption, and is disabled for
JSON, quiet, debug, and redirected output. Download completion is not verification:
the pinned SHA-256 must still match before the image is published.

Downloads use standard urllib environment proxy handling: `https_proxy` or
`HTTPS_PROXY`, with `no_proxy` or `NO_PROXY` for bypasses (including `*`).
Lowercase settings take precedence, as in urllib. An HTTP proxy may tunnel an
HTTPS download; TLS certificate verification stays enabled and redirects to HTTP
are rejected. Proxy use never changes the catalog digest or provenance checks.

`labctl image inspect -v NAME` additionally shows the verified cached blob
`path`, or `not cached`. Nonverbose and JSON inspection retain their existing
fields, even when `--json` is combined with `-v`.

## Import a trusted image

Use `labctl image import` when you need a different build or a custom image:

1. Obtain the QCOW2 from a source you authorise.
2. Obtain its published SHA-256 through an independent trusted channel. The
   digest must describe that exact build and architecture.
3. Verify the downloaded file before importing it. For example:

       printf '%s  %s\n' 'PUBLISHED_SHA256' './image.qcow2' | sha256sum --check -

4. Import the file under the logical ID used by your lab definition:

       labctl image import local:example ./image.qcow2 --checksum PUBLISHED_SHA256

5. Confirm that the image is cached:

       labctl image inspect local:example
       labctl image list --cached

`--checksum` must be 64 lowercase hexadecimal characters. During import, labctl
also runs `qemu-img info` and `qemu-img check`, requires QCOW2 format, hashes the
source, and refuses a mismatch. The imported file is copied into labctl's
content-addressed cache; the original download is not used after import.

The image ID matters. Importing an image as `local:rocky9` does not satisfy a
definition whose `image` field is `rocky:9`. Use the exact ID referenced by the
definition if the imported image is intended to satisfy it.

## Import without a trusted digest

This is supported for testing, but it does not establish publisher provenance:

    labctl image import local:example ./image.qcow2

Such an image is recorded as untrusted. Lab creation then fails closed unless
you explicitly accept it:

    labctl lab create LAB_ID --allow-untrusted-image

A digest calculated only from the downloaded file proves later file identity;
it does not prove who published the file. Pass `--checksum` only when the
expected value came from a source you trust independently of the image download.

## `pull` versus `import`

- `image pull NAME` downloads only a release pinned in labctl's bundled catalog
  by an exact HTTPS URL and SHA-256. Users cannot add pull URLs through runtime
  config.
- `image import NAME PATH --checksum SHA256` is the supported way to add a local,
  provenance-verified QCOW2 under any logical name.
- `image import NAME PATH` adds a structurally valid but untrusted QCOW2.

Maintainers who add or update a managed download must update
`src/labctl/data/images/metadata.json` with the exact release URL, digest, build
identity, architecture, checksum source URL, and default SSH user, then update
tests and release verification. Never add a moving `latest` URL or a digest
obtained only by hashing the same untrusted download.

## Storage behaviour

Available catalog entries require HTTPS and a 64-character lowercase SHA-256.
Pull downloads to a temporary file, verifies the digest, atomically publishes
an immutable blob, and writes a logical reference. Existing blobs are
re-hashed. Per-image locks serialise publication.

Every import runs `qemu-img info` and `qemu-img check`. A matching explicit
`--checksum` marks it verified; without one it is recorded untrusted and create
requires `--allow-untrusted-image`. Record where the image came from, its release
identity, retrieval time, and signature/checksum verification procedure outside
the cache. Never treat a filename as provenance.

The store layout is `blobs/sha256/DIGEST`, `refs/`, `tmp/`, and `locks/` beneath
`$XDG_CACHE_HOME/labctl/images` (normally `~/.cache/labctl/images`). Deleting with
force removes only the logical reference; immutable blobs remain for safe reuse.
Garbage collection is not implemented.

For a new KVM lab, each required cache blob is copied by digest into
`LIBVIRT_STORAGE_ROOT/INSTANCE_UID/bases/DIGEST` and the copy is re-hashed before
use. The overlay backs onto that copy. This keeps the cache and its private XDG
ancestors unavailable to system QEMU while retaining the cache as the verified
source for future lab creation. Reset verifies and reuses the per-instance copy.
