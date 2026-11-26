# Read-only source connectivity from RAM

This developer test extends the single-disk Ubuntu recovery rehearsal with a
separate disposable Ubuntu source VM. The packaged application performs both
the bootstrap source inventory and the later `recovery check-source` calls.
Neither the real source server nor its credentials are used.

Run as an unprivileged developer with the prerequisites for
[the Ubuntu recovery rehearsal](RECOVERY_JOBS.md):

```sh
python3 scripts/test_source_connectivity_vm.py \
  --image /private/cache/noble-server-cloudimg-amd64.img \
  --sha256 VERIFIED_IMAGE_SHA256 \
  --kernel /private/cache/vmlinuz-KERNEL_VERSION \
  --kernel-version KERNEL_VERSION
```

The harness verifies the supplied cloud image digest. Each VM receives one new
regular disk image; the destination has no second staging disk. Separate,
restricted user networks provide management SSH on random localhost ports. A
QEMU socket connection bound to loopback connects the guests' second NICs,
using static IPv4 addresses `192.0.2.10` and `192.0.2.20`. There is no host bridge,
LAN connection or real-server key. Source SSH trust is pinned to the generated
source host public key before either guest connects to it.

The RAM bundle includes OpenSSH and its shared libraries. Optional
`recovery build-vm-image --extra-network INTERFACE=IPv4/CIDR` entries configure
additional directly connected IPv4 interfaces. Names and addresses are validated
before shell generation. This is VM rehearsal networking, not hardware discovery
or a production network configuration installer.

The test checks:

- An actual source inventory supplies the draft plan before destination reboot.
- The RAM application collects another source inventory with destination root
  and `/boot` unmounted, using only its restored key and pinned trust file.
- A wrong source host key is rejected and recorded as a connection failure.
- A disconnected source link records failure without changing the recovery
  checkpoint, and another check succeeds when the link returns.
- After forced destination VM termination, the same job resumes and can collect
  source evidence again with the target unmounted.
- Corrupt-image fallback still returns to Ubuntu; destination GPT metadata,
  EFI bytes and root UUID remain unchanged after clean EFI unmount.

Private results and inventories remain under `reports/source-connectivity-*`.
The full rehearsal passed on 2026-11-26; local evidence is
`reports/source-connectivity-0791zysg/result.json`. Both RAM source inventories
completed with zero probe errors, including the check after forced destination
termination. The 131 automated tests passed under Python 3.12 and Python 3.8.10.

The source is queried through the existing in-memory collector; no collector
package or script is installed there. SSH audit logs and metadata reads may be
recorded, and the source VM naturally writes its own running OS state.

No files are migrated, no writers are stopped, and no imported OS is booted.
A reachable source is not a qualified migration. DNS, IPv6, DHCP, authentication
variants, physical NICs and the actual Ubuntu 20.04/ext3 source require further
coverage. Transfer, metadata preservation, interrupted-copy recovery, boot
adaptation and cutover remain separate development gates.
