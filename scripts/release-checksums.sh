#!/usr/bin/env bash
# Write SHA256SUMS for a release tag and attach it to the GitHub release.
#
# The Pi's updater downloads the tag's source tarball and, when the release
# has this SHA256SUMS asset, checks every file against it before update.sh
# runs. Without the asset the update still works (it is only not checked).
#
#   scripts/release-checksums.sh v1.13.0            # write ./SHA256SUMS and upload it
#   scripts/release-checksums.sh v1.13.0 --no-upload
set -euo pipefail
tag=${1:?usage: scripts/release-checksums.sh <tag> [--no-upload]}
out=${PWD}/SHA256SUMS
work=$(mktemp -d)
trap 'rm -rf -- "${work}"' EXIT
# The same tree GitHub's tarball for the tag contains (git archive).
git archive --format=tar "${tag}" | tar -x -C "${work}"
(cd "${work}" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sed 's#  \./#  #') > "${out}"
echo "Wrote ${out} ($(wc -l < "${out}") files) for ${tag}."
if [[ ${2:-} != --no-upload ]]; then
  gh release upload "${tag}" "${out}" --clobber
  echo "Uploaded SHA256SUMS to release ${tag}."
fi
