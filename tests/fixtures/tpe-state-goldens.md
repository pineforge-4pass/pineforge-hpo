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
samplers, including raw double bits. Version 0.9.0 intentionally regenerates the
serial oracle for TPE algorithm revision 2 after replacing host libm and extended
precision. The portable stream is checked against the serial and threaded paths,
not inherited as an assertion of v0.8.0 proposal compatibility.

| Numerical contract | Eight-suggestion SHA-256 |
| --- | --- |
| `portable-tpe-v2;binary64:53` | `3e093fefd5c00f9241a115ef5be729e42729bff41a30d3253485c43110451810` |

`tpe-serial-goldens.txt` now keys the hash by portable algorithm revision and binary64
precision, not host long-double precision. The fixture file SHA-256 is
`aa8070c4a24abb5c03e7bff25a7cba7488267064cb9d7dd657c533887b3f2971`.
Every golden run prints its key and full numerical identity. Unknown contract keys
return CTest skip code 77 only outside CI. Configure with
`-DPINEFORGE_HPO_REQUIRE_SERIAL_GOLDEN=ON` (both native CI jobs do) to make any
missing golden a failure. Refresh proposal hashes only with an intentional numerical
algorithm revision and independent serial/threaded verification; ordinary identity
changes must not rebaseline them. The [eight-space cross-vendor proof](../../docs/portable-math.md)
also checks Intel, AMD, aarch64 Linux and macOS arm64 over 256 newly proposed trials
per space and imports actual Intel checkpoints on the other three hosts.

`checkpoint_exchange` writes or imports a full bounded `PFHTPE2` checkpoint for
201 deterministic rows and a seven-element reservoir. Exchanging matching revision-2
macOS and Linux files must report `restored_sampler_state`, just like same-host round
trips. Revision-1/v0.8.0 files instead rebuild ordered history with a clear numerical
compatibility reason; their old sampler state is never mixed into revision 2.
