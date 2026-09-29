/* Correctness / eligibility probe for the issue-#144 mmap interposer.
 *
 * Exercises, in one process:
 *   1. large anonymous RW mapping (>= min bytes) -> must be redirected
 *      (backing file visible in /proc/self/maps, data round-trips).
 *   2. small anonymous RW mapping -> must bypass (too_small).
 *   3. large anonymous PROT_READ-only mapping -> must bypass (no_write).
 *   4. large anonymous PROT_EXEC mapping -> must bypass (executable).
 *   5. MAP_FIXED mapping -> must bypass (fixed).
 *   6. file-backed (non-anonymous) mapping -> must bypass (not_anonymous).
 *   7. large MAP_SHARED anonymous mapping -> must bypass (shared_anon).
 *   8. fork() CoW check: child writes a redirected mapping, parent must
 *      observe unchanged bytes (fails closed under MAP_SHARED mode, which
 *      is exactly the documented hazard of that mode).
 *   9. munmap of a redirected mapping must succeed.
 *
 * Exit 0 only when every expectation holds; prints TAP-ish lines.
 * The harness additionally asserts on the emitted stats JSON
 * (redirected_bytes material, bypass reasons present).
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static int failures = 0;

#define CHECK(cond, label)                                                  \
    do {                                                                    \
        if (cond) {                                                         \
            printf("ok %s\n", label);                                       \
        } else {                                                            \
            printf("FAIL %s (line %d errno=%d)\n", label, __LINE__, errno); \
            failures++;                                                     \
        }                                                                   \
    } while (0)

/* Returns 1 when addr is mapped onto the shim backing file. */
static int maps_has_backing(void *addr, const char *token) {
    FILE *f = fopen("/proc/self/maps", "r");
    char line[1024];
    unsigned long lo, hi;
    int found = 0;
    if (!f)
        return -1;
    while (fgets(line, sizeof(line), f)) {
        if (sscanf(line, "%lx-%lx", &lo, &hi) != 2)
            continue;
        if ((uintptr_t)addr >= lo && (uintptr_t)addr < hi) {
            found = (strstr(line, token) != NULL);
            break;
        }
    }
    fclose(f);
    return found;
}

int main(int argc, char **argv) {
    const char *token = argc > 1 ? argv[1] : "mmapfb.pool";
    const size_t big = 2 * 1024 * 1024;
    const size_t small = 64 * 1024;
    void *p_big, *p_small, *p_ro, *p_exec, *p_file, *p_shared, *p_fixed_base;
    void *p_fixed;
    int fd;
    char tmpl[] = "/tmp/mmapfb-test-XXXXXX";
    pid_t pid;
    int status;

    /* 1. large anon RW -> redirected, zero-initialised, writable. */
    p_big = mmap(NULL, big, PROT_READ | PROT_WRITE,
                 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    CHECK(p_big != MAP_FAILED, "big-anon-rw-mapped");
    if (p_big != MAP_FAILED) {
        int zero = 1;
        size_t i;
        unsigned char *b = p_big;
        for (i = 0; i < big; i += 4096) {
            if (b[i] != 0) {
                zero = 0;
                break;
            }
        }
        CHECK(zero, "big-anon-rw-zero-initialised");
        memset(p_big, 0xAB, big);
        CHECK(((unsigned char *)p_big)[big - 1] == 0xAB,
              "big-anon-rw-writable");
        CHECK(maps_has_backing(p_big, token) == 1,
              "big-anon-rw-file-backed");
    }

    /* 2. small anon RW -> anonymous (bypass). */
    p_small = mmap(NULL, small, PROT_READ | PROT_WRITE,
                   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    CHECK(p_small != MAP_FAILED, "small-anon-rw-mapped");
    if (p_small != MAP_FAILED)
        CHECK(maps_has_backing(p_small, token) == 0,
              "small-anon-rw-stays-anonymous");

    /* 3. large read-only anon -> bypass. */
    p_ro = mmap(NULL, big, PROT_READ, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    CHECK(p_ro != MAP_FAILED, "big-anon-ro-mapped");
    if (p_ro != MAP_FAILED)
        CHECK(maps_has_backing(p_ro, token) == 0,
              "big-anon-ro-stays-anonymous");

    /* 4. large executable anon -> bypass. */
    p_exec = mmap(NULL, big, PROT_READ | PROT_WRITE | PROT_EXEC,
                  MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    CHECK(p_exec != MAP_FAILED, "big-anon-exec-mapped");
    if (p_exec != MAP_FAILED)
        CHECK(maps_has_backing(p_exec, token) == 0,
              "big-anon-exec-stays-anonymous");

    /* 5. MAP_FIXED large anon RW -> bypass. Reserve then fix. */
    p_fixed_base = mmap(NULL, big, PROT_NONE,
                        MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE, -1, 0);
    CHECK(p_fixed_base != MAP_FAILED, "fixed-reserve-mapped");
    if (p_fixed_base != MAP_FAILED) {
        p_fixed = mmap(p_fixed_base, big, PROT_READ | PROT_WRITE,
                       MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED, -1, 0);
        CHECK(p_fixed == p_fixed_base, "fixed-remap-ok");
        if (p_fixed == p_fixed_base)
            CHECK(maps_has_backing(p_fixed, token) == 0,
                  "fixed-stays-anonymous");
    }

    /* 6. real file mapping (not anonymous) -> bypass. */
    fd = mkstemp(tmpl);
    CHECK(fd >= 0, "tmpfile-created");
    if (fd >= 0) {
        unlink(tmpl);
        CHECK(ftruncate(fd, (off_t)big) == 0, "tmpfile-sized");
        p_file = mmap(NULL, big, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
        CHECK(p_file != MAP_FAILED, "file-shared-mapped");
        if (p_file != MAP_FAILED)
            CHECK(maps_has_backing(p_file, token) == 0,
                  "file-mapping-untouched");
        close(fd);
    }

    /* 7. MAP_SHARED anonymous -> bypass (sharing contract preserved). */
    p_shared = mmap(NULL, big, PROT_READ | PROT_WRITE,
                    MAP_SHARED | MAP_ANONYMOUS, -1, 0);
    CHECK(p_shared != MAP_FAILED, "shared-anon-mapped");
    if (p_shared != MAP_FAILED)
        CHECK(maps_has_backing(p_shared, token) == 0,
              "shared-anon-stays-anonymous");

    /* 8. fork CoW: child dirties the redirected mapping; parent bytes
     * must be unchanged (guards the MAP_SHARED fork-sharing hazard). */
    if (p_big != MAP_FAILED) {
        memset(p_big, 0xCD, big);
        pid = fork();
        CHECK(pid >= 0, "fork-ok");
        if (pid == 0) {
            memset(p_big, 0x11, big);
            _exit(((unsigned char *)p_big)[0] == 0x11 ? 0 : 2);
        } else if (pid > 0) {
            CHECK(waitpid(pid, &status, 0) == pid, "child-reaped");
            CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 0,
                  "child-saw-own-write");
            CHECK(((unsigned char *)p_big)[0] == (unsigned char)0xCD,
                  "parent-bytes-unchanged-after-fork");
        }
    }

    /* 9. munmap of the redirected mapping succeeds. */
    if (p_big != MAP_FAILED)
        CHECK(munmap(p_big, big) == 0, "redirected-munmap-ok");
    if (p_small != MAP_FAILED)
        CHECK(munmap(p_small, small) == 0, "small-munmap-ok");
    if (p_ro != MAP_FAILED)
        CHECK(munmap(p_ro, big) == 0, "ro-munmap-ok");
    if (p_exec != MAP_FAILED)
        CHECK(munmap(p_exec, big) == 0, "exec-munmap-ok");

    if (failures == 0)
        printf("RESULT PASS\n");
    else
        printf("RESULT FAIL failures=%d\n", failures);
    return failures == 0 ? 0 : 1;
}
