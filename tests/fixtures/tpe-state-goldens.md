# TPE checkpoint and serial suggestion goldens

`tpe-mt64-golden.txt` is the canonical two-engine state checkpoint shared by the
Ubuntu/libstdc++ and macOS/libc++ native CI legs. The first MT19937-64 engine has
seed 17 and has consumed 999 draws; the second has seed `0x9e3779b97f4a7c15` and
has consumed 201 draws. Each line has `MT64 312`, 312 uint64 state words, and an
explicit position. No standard-library stream format is involved.

`test_mt_state.cpp` restores both and verifies all 4,096 subsequent words per
engine against the native standard engine and this platform-independent SHA-256:
`3132b2ec79fbb1b9741d1c771356ba94ffce7e41c5e172e442ddc5350fb6db70`.
The hashed transcript is each uint64 in decimal followed by one ASCII space.
The fixture was emitted on Apple clang/libc++, independently checked against
native draws, and restored on GCC/libstdc++ with the same future-word hash.

`parallel_checkpoint_equivalence()` imports 4,200 deterministic mixed/log rows,
then compares eight pending suggestions from explicitly serial and eight-worker
samplers, including raw double bits. Its expected transcript hashes were derived
from the unmodified v0.6.0 serial source (`6fc5b1fe`), not the new threaded path:

| Numerical build | Eight-suggestion SHA-256 |
| --- | --- |
| Linux GCC/libstdc++/glibc | `f7a777464e1d8731c3db660bf1671c2bf884332705598253c88b0e1521c9d4bf` |
| macOS Apple clang/libc++/libSystem | `3e093fefd5c00f9241a115ef5be729e42729bff41a30d3253485c43110451810` |

Both use `-ffp-contract=off`. The math-library difference is why numerical-build
identity is enforced for full sampler checkpoints: canonical RNG transport does
not make different floating-point/math-library builds proposal-equivalent.

`checkpoint_exchange` writes or imports a full bounded `PFHTPE2` checkpoint for
201 deterministic rows and a seven-element reservoir. Exchanging macOS and Linux
files must report `rebuilt_history` with exit zero; same-build round trips must
report `restored_sampler_state`. Neither foreign build may return exit 4.
