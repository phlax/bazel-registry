# libevent module maintainer notes

## Android config header regeneration

`/home/runner/work/bazel-registry/bazel-registry/modules/libevent/2.2.2-alpha.envoy/overlay/event2-config_android.h`
was generated from libevent `release-2.2.2-alpha` using:

- Android NDK: `r26d`
- Android API level: `23` (Envoy Mobile minimum SDK at time of generation)

Regenerate when libevent changes or when Envoy Mobile raises min SDK:

```bash
NDK=/path/to/android-ndk-r26d
cmake /path/to/libevent-release-2.2.2-alpha \
  -DCMAKE_TOOLCHAIN_FILE="$NDK/build/cmake/android.toolchain.cmake" \
  -DANDROID_ABI=x86_64 \
  -DANDROID_PLATFORM=android-23 \
  -DEVENT__DISABLE_TESTS=ON \
  -DEVENT__DISABLE_SAMPLES=ON \
  -DEVENT__DISABLE_BENCHMARK=ON
```

Then copy generated `include/event2/event-config.h` values into the Bazel overlay header in the same style as other platform config headers.

`evconfig-private_android.h` should be kept aligned with generated
`include/evconfig-private.h`; for Android API 23 it currently matches
the linux private config.
