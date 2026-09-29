/* Experimental LD_PRELOAD mmap interposer: redirect large anonymous RW
 * mappings to a disk-backed pool file (issue #144).
 *
 * Goal: make the memory class that malloc-family interposition
 * (libvmmalloc, issue #56) cannot catch -- Bun/JavaScriptCore large
 * anonymous mmap allocations -- experimentally file-backed on ordinary
 * ephemeral disk, then measure the exact OpenCode PR #15 coding workload
 * under 512 MiB / no swap.
 *
 * This is a measurement probe, NOT a production allocator. It is strictly
 * opt-in: when OPENCODE_FILEBACKED_DIR is unset or empty, every call is a
 * pure passthrough to the real libc implementation.
 *
 * Build:
 *   gcc -O2 -shared -fPIC -Wall -Werror -o libmmapfb.so shim.c -ldl -lpthread
 *
 * Environment:
 *   OPENCODE_FILEBACKED_DIR        directory for the backing file (required;
 *                                  unset/empty disables the shim entirely)
 *   OPENCODE_FILEBACKED_MIN_BYTES  minimum mapping size to redirect
 *                                  (default 1048576, floor 4096)
 *   OPENCODE_FILEBACKED_SIZE_MB    sparse pool file size in MiB
 *                                  (default 2048, clamped 64..16384)
 *   OPENCODE_FILEBACKED_SHARED     0 = MAP_PRIVATE file mapping (default,
 *                                  safest: preserves copy-on-write/fork
 *                                  semantics of anonymous memory);
 *                                  1 = MAP_SHARED file mapping (swap-like
 *                                  spill, but shared across fork -- see the
 *                                  semantics warning below)
 *   OPENCODE_FILEBACKED_STATS      stats JSON path
 *                                  (default $DIR/mmapfb-stats-<pid>.json)
 *
 * Eligibility (redirect ONLY when ALL hold):
 *   - MAP_ANONYMOUS (or MAP_ANON) is set
 *   - MAP_PRIVATE is set (shared-anon mappings are never redirected:
 *     remapping them onto unique file offsets would silently break the
 *     cross-process sharing contract)
 *   - PROT_WRITE is set (writable data mappings only)
 *   - PROT_EXEC is NOT set (never touch executable mappings)
 *   - MAP_FIXED / MAP_FIXED_NOREPLACE are NOT set
 *   - MAP_STACK / MAP_GROWSDOWN are NOT set
 *   - fd == -1 (anonymous convention; conservative)
 *   - length >= OPENCODE_FILEBACKED_MIN_BYTES (default 1 MiB)
 *
 * Fail-closed behaviour: any condition that prevents a safe redirection
 * (shim disabled, init failure, pool exhaustion, tracking failure,
 * reentrant call during libc symbol resolution) falls back to the original
 * anonymous mmap rather than crashing the process.
 *
 * Semantics warning: a redirected mapping is backed by a regular file, so
 * it appears as file-backed (not anonymous) in /proc/<pid>/smaps. The
 * default MAP_PRIVATE mode keeps copy-on-write/fork semantics identical to
 * anonymous memory: pages start zero-filled (fresh sparse-file holes read
 * as zero, offsets are never reused within a process), writes stay private
 * to the mapping, and nothing is shared with other processes. The optional
 * MAP_SHARED mode additionally lets dirty pages be written back and
 * reclaimed like swap, but a fork() child then shares those pages with the
 * parent (writes become visible across the fork), which breaks the
 * copy-on-write contract anonymous memory normally provides. Do NOT enable
 * MAP_SHARED mode unless smoke/correctness tests for the target binary
 * prove the sharing hazard does not fire (the bundled fork test fails
 * closed on exactly this hazard).
 *
 * Counters: intercepted calls, redirected count/bytes, bypassed bytes
 * classified by reason, and backing-file bytes allocated are dumped as JSON
 * to OPENCODE_FILEBACKED_STATS by a destructor, plus a one-line stderr
 * summary prefixed with "[mmapfb]".
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#ifndef MAP_ANON
#define MAP_ANON 0
#endif
#ifndef MAP_FIXED_NOREPLACE
#define MAP_FIXED_NOREPLACE 0
#endif
#ifndef MAP_STACK
#define MAP_STACK 0
#endif

/* Bypass reason codes (also used as JSON keys for bypass_by_reason). */
#define REASON_DISABLED "disabled"
#define REASON_NOT_ANONYMOUS "not_anonymous"
#define REASON_SHARED_ANON "shared_anon"
#define REASON_HAS_FD "has_fd"
#define REASON_NO_WRITE "no_write"
#define REASON_EXECUTABLE "executable"
#define REASON_FIXED "fixed"
#define REASON_STACK "stack"
#define REASON_TOO_SMALL "too_small"
#define REASON_INIT_FAILED "init_failed"
#define REASON_POOL_EXHAUSTED "pool_exhausted"
#define REASON_RESOLVE_REENTRY "resolve_reentry"
#define REASON_POST_FORK "post_fork"
#define N_REASONS 13

static const char *k_reasons[N_REASONS] = {
    REASON_DISABLED,     REASON_NOT_ANONYMOUS, REASON_SHARED_ANON,
    REASON_HAS_FD,       REASON_NO_WRITE,      REASON_EXECUTABLE,
    REASON_FIXED,        REASON_STACK,         REASON_TOO_SMALL,
    REASON_INIT_FAILED,  REASON_POOL_EXHAUSTED, REASON_RESOLVE_REENTRY,
    REASON_POST_FORK,
};

typedef struct {
    unsigned long long intercepted;
    unsigned long long redirected_count;
    unsigned long long redirected_bytes;
    unsigned long long active_count;
    unsigned long long active_bytes;
    unsigned long long bypassed_total;
    unsigned long long bypassed_bytes;
    unsigned long long by_reason[N_REASONS];
    unsigned long long backing_bytes_allocated;
} shim_stats_t;

typedef struct {
    void *addr;
    size_t len;
} tracked_t;

static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static __thread int t_in_shim = 0;

/* Real libc implementations resolved via dlsym(RTLD_NEXT, ...). */
static void *(*real_mmap)(void *, size_t, int, int, int, off_t) = NULL;
static void *(*real_mmap64)(void *, size_t, int, int, int, off_t) = NULL;
static int (*real_munmap)(void *, size_t) = NULL;

/* Pool state: 0 = uninitialised, 1 = ready, -1 = unavailable (fail closed). */
static int g_state = 0;
static int g_resolving = 0;
/* Set in the child by the pthread_atfork handler: a forked child must
 * never reuse the parent's bump-pointer file offsets (the file ranges may
 * already hold the parent's dirty bytes, which would break the
 * zero-initialised private-memory contract). Children therefore fall back
 * to plain anonymous mmap. */
static int g_post_fork = 0;
static int g_atfork_installed = 0;
static int g_flusher_started = 0;
/* 2048 keeps "%s/mmapfb-stats-<pid>.json" provably fittable in 4096. */
static char g_backing_dir[2048];
static char g_custom_stats[4096];
static int g_pool_fd = -1;
static char g_pool_path[4096];
static size_t g_pool_size = 0;
static size_t g_pool_next = 0;
static size_t g_min_bytes = 1048576;
static int g_shared_mode = 0;
static long g_pagesize = 4096;

static shim_stats_t g_stats;

static int reason_index(const char *reason) {
    int i;
    for (i = 0; i < N_REASONS; i++) {
        if (strcmp(k_reasons[i], reason) == 0)
            return i;
    }
    return -1;
}

/* Caller must hold g_lock. */
static void record_bypass_locked(const char *reason, size_t len) {
    int idx = reason_index(reason);
    g_stats.bypassed_total++;
    g_stats.bypassed_bytes += (unsigned long long)len;
    if (idx >= 0)
        g_stats.by_reason[idx]++;
}

/* Direct anonymous mmap via raw syscall. Used ONLY to satisfy reentrant
 * mmap requests that arrive while dlsym() is resolving the real symbols
 * (libc internals may mmap during symbol lookup). This keeps the loader
 * alive without recursing; every normal call goes through real_mmap. */
static void *raw_anon_mmap(size_t len) {
    long ret = syscall(SYS_mmap, NULL, len, PROT_READ | PROT_WRITE,
                       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (ret < 0 && ret > -4096)
        return MAP_FAILED;
    return (void *)ret;
}

static void ensure_real_locked(void) {
    if (real_mmap && real_munmap)
        return;
    if (g_resolving)
        return;
    g_resolving = 1;
    /* dlsym may reenter this translation unit (malloc/mmap inside libc);
     * the reentrancy guards in mmap()/munmap() cover that window. */
    if (!real_mmap)
        real_mmap = (void *(*)(void *, size_t, int, int, int, off_t))dlsym(
            RTLD_NEXT, "mmap");
    if (!real_mmap64)
        real_mmap64 = (void *(*)(void *, size_t, int, int, int, off_t))dlsym(
            RTLD_NEXT, "mmap64");
    if (!real_munmap)
        real_munmap = (int (*)(void *, size_t))dlsym(RTLD_NEXT, "munmap");
    if (!real_mmap64)
        real_mmap64 = real_mmap;
    g_resolving = 0;
}

static size_t align_up(size_t n, size_t a) { return (n + a - 1) & ~(a - 1); }

static long parse_long_env(const char *name, long dflt, long lo, long hi) {
    const char *s = getenv(name);
    long v;
    char *end = NULL;
    if (!s || !*s)
        return dflt;
    v = strtol(s, &end, 10);
    if (end == s || v < lo)
        return dflt;
    if (v > hi)
        return hi;
    return v;
}

/* Caller must hold g_lock. Resolves symbols and creates the sparse pool
 * file on first use. Never crashes: any failure leaves g_state = -1 so
 * all future calls transparently fall back to anonymous mmap. */
static void init_once_locked(void) {
    const char *dir;
    long size_mb;
    long min_bytes;
    int fd;
    char path[4096];
    struct stat st;

    if (g_state != 0)
        return;
    ensure_real_locked();
    if (!real_mmap || !real_munmap) {
        g_state = -1;
        return;
    }
    dir = getenv("OPENCODE_FILEBACKED_DIR");
    if (!dir || !*dir) {
        g_state = -1;
        return;
    }
    g_pagesize = sysconf(_SC_PAGESIZE);
    if (g_pagesize <= 0)
        g_pagesize = 4096;
    min_bytes =
        parse_long_env("OPENCODE_FILEBACKED_MIN_BYTES", 1048576, 4096,
                       1L << 31);
    g_min_bytes = (size_t)min_bytes;
    g_shared_mode = parse_long_env("OPENCODE_FILEBACKED_SHARED", 0, 0, 1) == 1;
    size_mb = parse_long_env("OPENCODE_FILEBACKED_SIZE_MB", 2048, 64, 16384);
    g_pool_size = (size_t)size_mb * 1024 * 1024;

    snprintf(path, sizeof(path), "%s/mmapfb.pool.%d", dir, (int)getpid());
    fd = open(path, O_RDWR | O_CREAT | O_EXCL, 0600);
    if (fd < 0) {
        g_state = -1;
        return;
    }
    /* Pre-size the sparse file once so every fresh offset reads as zero
     * without ever reusing (and leaking) a previously written range. */
    if (ftruncate(fd, (off_t)g_pool_size) != 0) {
        close(fd);
        unlink(path);
        g_state = -1;
        return;
    }
    if (fstat(fd, &st) != 0) {
        close(fd);
        unlink(path);
        g_state = -1;
        return;
    }
    snprintf(g_pool_path, sizeof(g_pool_path), "%s", path);
    /* Keep room for the "/mmapfb-stats-<pid>.json" suffix so the per-flush
     * path computation below cannot truncate (fail closed, no -Werror). */
    snprintf(g_backing_dir, sizeof(g_backing_dir), "%s", dir);
    g_backing_dir[sizeof(g_backing_dir) - 1] = '\0';
    {
        const char *stats = getenv("OPENCODE_FILEBACKED_STATS");
        if (stats && *stats)
            snprintf(g_custom_stats, sizeof(g_custom_stats), "%s", stats);
        else
            g_custom_stats[0] = '\0';
    }
    g_pool_fd = fd;
    g_pool_next = 0;
    g_state = 1;
}

/* Stats path for the CURRENT process: an explicit OPENCODE_FILEBACKED_STATS
 * path is honoured verbatim, otherwise $DIR/mmapfb-stats-<pid>.json (per
 * pid so forked children never clobber the parent's telemetry). */
static void current_stats_path(char *out, size_t outlen) {
    if (g_custom_stats[0])
        snprintf(out, outlen, "%s", g_custom_stats);
    else
        snprintf(out, outlen, "%s/mmapfb-stats-%d.json", g_backing_dir,
                 (int)getpid());
}

static void write_stats_file(void); /* forward */
static void atfork_child(void);     /* forward */

static void *flusher_main(void *arg) {
    (void)arg;
    for (;;) {
        sleep(2);
        /* Best effort: telemetry must never break the process. A raw
         * exit_group (as used by the Bun/Zig runtime) skips shared-library
         * destructors, so without this periodic flush no counters would
         * ever reach disk. */
        pthread_mutex_lock(&g_lock);
        if (g_state == 1 && !g_post_fork) {
            pthread_mutex_unlock(&g_lock);
            write_stats_file();
        } else {
            pthread_mutex_unlock(&g_lock);
        }
    }
    return NULL;
}

/* Start the telemetry flusher without holding g_lock (pthread_create may
 * mmap on this thread). Idempotent: the started flag is claimed under the
 * lock, the thread is created outside it. */
static void ensure_flusher(void) {
    int need = 0;
    pthread_t tid;
    pthread_attr_t attr;
    pthread_mutex_lock(&g_lock);
    if (g_state == 1 && !g_post_fork && !g_flusher_started) {
        g_flusher_started = 1;
        need = 1;
    }
    if (g_state == 1 && !g_atfork_installed) {
        g_atfork_installed = 1;
        need |= 2;
    }
    pthread_mutex_unlock(&g_lock);
    if (need & 2)
        pthread_atfork(NULL, NULL, atfork_child);
    if (!(need & 1))
        return;
    if (pthread_attr_init(&attr) != 0)
        goto fail;
    pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);
    if (pthread_create(&tid, &attr, flusher_main, NULL) != 0) {
        pthread_attr_destroy(&attr);
        goto fail;
    }
    pthread_attr_destroy(&attr);
    return;
fail:
    pthread_mutex_lock(&g_lock);
    g_flusher_started = 0;
    pthread_mutex_unlock(&g_lock);
}

/* Fork child: never reuse the parent's file offsets (see g_post_fork). The
 * flusher thread does not survive fork; the child runs anonymous-only, so
 * no replacement flusher is needed. Mutex state is re-initialised because
 * another thread may have held the lock across the fork. */
static void atfork_child(void) {
    g_post_fork = 1;
    g_flusher_started = 0;
    pthread_mutex_init(&g_lock, NULL);
}

/* Decide eligibility. Returns NULL when eligible, else the bypass reason.
 * Pure function of (prot, flags, fd, len); unit-tested via the C test prog. */
static const char *eligibility(int prot, int flags, int fd, size_t len,
                               size_t min_bytes) {
    if (!(flags & (MAP_ANONYMOUS | MAP_ANON)))
        return REASON_NOT_ANONYMOUS;
    if (!(flags & MAP_PRIVATE))
        return REASON_SHARED_ANON;
    if (fd != -1)
        return REASON_HAS_FD;
    if (!(prot & PROT_WRITE))
        return REASON_NO_WRITE;
    if (prot & PROT_EXEC)
        return REASON_EXECUTABLE;
    if (flags & (MAP_FIXED | MAP_FIXED_NOREPLACE))
        return REASON_FIXED;
    if (flags & (MAP_STACK | MAP_GROWSDOWN))
        return REASON_STACK;
    if ((size_t)len < min_bytes)
        return REASON_TOO_SMALL;
    return NULL;
}

/* Fixed static tracking table: avoids any malloc-while-locked deadlock
 * (real malloc may reenter our mmap on the same thread, and our mutex is
 * not recursive). 16384 slots cover the mapping counts observed for the
 * OpenCode/Bun workload; overflow stays correct (mapping valid, just
 * untracked for the active-byte counter). */
#define TRACK_CAP 16384
static tracked_t g_slots[TRACK_CAP];
static size_t g_slots_len = 0;

static int track_slot(void *addr, size_t len) {
    size_t i;
    if (g_slots_len < TRACK_CAP) {
        g_slots[g_slots_len].addr = addr;
        g_slots[g_slots_len].len = len;
        g_slots_len++;
        return 1;
    }
    for (i = 0; i < TRACK_CAP; i++) {
        if (g_slots[i].addr == NULL) {
            g_slots[i].addr = addr;
            g_slots[i].len = len;
            return 1;
        }
    }
    return 0;
}

static int untrack_slot(void *addr, size_t len) {
    size_t i;
    (void)len;
    for (i = 0; i < g_slots_len; i++) {
        if (g_slots[i].addr == addr) {
            g_slots[i] = g_slots[g_slots_len - 1];
            g_slots[g_slots_len - 1].addr = NULL;
            g_slots[g_slots_len - 1].len = 0;
            g_slots_len--;
            return 1;
        }
    }
    return 0;
}

/* Throttled synchronous flush: guarantees the stats file on disk reflects
 * recent calls even for short-lived raw-exit processes (Bun/Zig skips
 * shared-library destructors via exit_group, and may exit before the 2 s
 * periodic flusher fires). Bounded to ~2 writes/s plus the first three
 * intercepted calls, so hot mmap loops pay almost nothing (clock_gettime
 * is vDSO). Must be called WITHOUT g_lock held. */
static unsigned long long g_last_flush_ms = 0;

static unsigned long long now_ms(void) {
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0)
        return 0;
    return (unsigned long long)ts.tv_sec * 1000ULL +
           (unsigned long long)ts.tv_nsec / 1000000ULL;
}

static void maybe_flush(void) {
    unsigned long long now;
    int due = 0;
    pthread_mutex_lock(&g_lock);
    now = now_ms();
    if (g_state == 1 && !g_post_fork &&
        (g_stats.intercepted <= 3 || now - g_last_flush_ms >= 500 ||
         now < g_last_flush_ms)) {
        g_last_flush_ms = now;
        due = 1;
    }
    pthread_mutex_unlock(&g_lock);
    if (due)
        write_stats_file();
}

static void *mmap_inner(void *addr, size_t len, int prot, int flags, int fd,
                        off_t offset) {
    const char *reason;
    void *p;
    int file_flags;
    size_t aligned;
    off_t file_off;

    if (t_in_shim) {
        /* Reentrant call (e.g. from inside dlsym): satisfy small requests
         * with a raw anonymous mapping so libc initialisation can proceed.
         * Count them so the stats stay honest. */
        pthread_mutex_lock(&g_lock);
        g_stats.intercepted++;
        record_bypass_locked(REASON_RESOLVE_REENTRY, len);
        pthread_mutex_unlock(&g_lock);
        return raw_anon_mmap(len ? len : (size_t)g_pagesize);
    }
    t_in_shim = 1;
    pthread_mutex_lock(&g_lock);
    g_stats.intercepted++;
    init_once_locked();
    {
        int ready = (g_state == 1);
        pthread_mutex_unlock(&g_lock);
        /* Outside the lock: pthread_create/pthread_atfork may allocate
         * (malloc -> mmap) on this thread, which must be able to take
         * g_lock. */
        if (ready)
            ensure_flusher();
        pthread_mutex_lock(&g_lock);
    }
    if (g_post_fork) {
        record_bypass_locked(REASON_POST_FORK, len);
        ensure_real_locked();
        p = real_mmap ? real_mmap(addr, len, prot, flags, fd, offset)
                      : MAP_FAILED;
        pthread_mutex_unlock(&g_lock);
        t_in_shim = 0;
        return p;
    }
    if (g_state != 1) {
        const char *r =
            (getenv("OPENCODE_FILEBACKED_DIR") &&
             *getenv("OPENCODE_FILEBACKED_DIR"))
                ? REASON_INIT_FAILED
                : REASON_DISABLED;
        record_bypass_locked(r, len);
        ensure_real_locked();
        p = real_mmap ? real_mmap(addr, len, prot, flags, fd, offset)
                      : MAP_FAILED;
        pthread_mutex_unlock(&g_lock);
        t_in_shim = 0;
        return p;
    }
    reason = eligibility(prot, flags, fd, len, g_min_bytes);
    if (reason != NULL) {
        record_bypass_locked(reason, len);
        p = real_mmap(addr, len, prot, flags, fd, offset);
        pthread_mutex_unlock(&g_lock);
        t_in_shim = 0;
        return p;
    }
    aligned = align_up(len, (size_t)g_pagesize);
    if (g_pool_next + aligned > g_pool_size || g_pool_next + aligned < g_pool_next) {
        record_bypass_locked(REASON_POOL_EXHAUSTED, len);
        p = real_mmap(addr, len, prot, flags, fd, offset);
        pthread_mutex_unlock(&g_lock);
        t_in_shim = 0;
        return p;
    }
    file_off = (off_t)g_pool_next;
    g_pool_next += aligned;
    /* Strip MAP_ANONYMOUS; keep every other flag (notably MAP_PRIVATE by
     * default, or MAP_SHARED when the operator explicitly opted in). */
    file_flags = flags & ~(MAP_ANONYMOUS | MAP_ANON);
    if (g_shared_mode) {
        file_flags &= ~MAP_PRIVATE;
        file_flags |= MAP_SHARED;
    } else {
        file_flags &= ~MAP_SHARED;
        file_flags |= MAP_PRIVATE;
    }
    p = real_mmap(NULL, len, prot, file_flags, g_pool_fd, file_off);
    if (p == MAP_FAILED) {
        /* Do not leak the reserved range accounting on failure; roll the
         * bump pointer back when it is still the tail (the common case). */
        if ((size_t)file_off + aligned == g_pool_next)
            g_pool_next = (size_t)file_off;
        /* Fall back to the original anonymous mapping: fail closed. */
        p = real_mmap(addr, len, prot, flags, fd, offset);
        if (p != MAP_FAILED)
            record_bypass_locked(REASON_INIT_FAILED, len);
        pthread_mutex_unlock(&g_lock);
        t_in_shim = 0;
        return p;
    }
    if (track_slot(p, len)) {
        g_stats.active_count++;
        g_stats.active_bytes += (unsigned long long)len;
    }
    g_stats.redirected_count++;
    g_stats.redirected_bytes += (unsigned long long)len;
    g_stats.backing_bytes_allocated = (unsigned long long)g_pool_next;
    pthread_mutex_unlock(&g_lock);
    t_in_shim = 0;
    return p;
}

void *mmap(void *addr, size_t length, int prot, int flags, int fd,
           off_t offset) {
    void *p;
    /* Zero-length mmap is invalid; let libc report the error. */
    if (length == 0) {
        pthread_mutex_lock(&g_lock);
        ensure_real_locked();
        pthread_mutex_unlock(&g_lock);
        if (real_mmap)
            return real_mmap(addr, length, prot, flags, fd, offset);
        errno = EINVAL;
        return MAP_FAILED;
    }
    p = mmap_inner(addr, length, prot, flags, fd, offset);
    maybe_flush();
    return p;
}

void *mmap64(void *addr, size_t length, int prot, int flags, int fd,
             off_t offset) {
    void *p;
    if (length == 0) {
        pthread_mutex_lock(&g_lock);
        ensure_real_locked();
        if (!real_mmap64)
            real_mmap64 = real_mmap;
        pthread_mutex_unlock(&g_lock);
        if (real_mmap64)
            return real_mmap64(addr, length, prot, flags, fd, offset);
        errno = EINVAL;
        return MAP_FAILED;
    }
    p = mmap_inner(addr, length, prot, flags, fd, offset);
    maybe_flush();
    return p;
}

int munmap(void *addr, size_t length) {
    int rc;
    if (t_in_shim) {
        long r = syscall(SYS_munmap, addr, length);
        return (int)r;
    }
    t_in_shim = 1;
    pthread_mutex_lock(&g_lock);
    ensure_real_locked();
    if (untrack_slot(addr, length)) {
        if (g_stats.active_count > 0)
            g_stats.active_count--;
        if (g_stats.active_bytes >= (unsigned long long)length)
            g_stats.active_bytes -= (unsigned long long)length;
        else
            g_stats.active_bytes = 0;
    }
    rc = real_munmap ? real_munmap(addr, length) : -1;
    if (!real_munmap)
        errno = ENOSYS;
    pthread_mutex_unlock(&g_lock);
    t_in_shim = 0;
    return rc;
}

/* Dump machine-readable counters. Uses only async-safe-ish syscalls plus
 * dlsym-free paths; failures are swallowed (telemetry must never break
 * process exit). Called periodically by the flusher thread (the primary
 * path: runtimes such as Bun/Zig terminate via raw exit_group, which
 * skips shared-library destructors) and once more, best effort, from the
 * destructor. */
static void write_stats_file(void) {
    char buf[8192];
    char stats_path[4096];
    int len = 0;
    int i;
    int fd;

    pthread_mutex_lock(&g_lock);
    if (g_state != 1 || g_post_fork) {
        pthread_mutex_unlock(&g_lock);
        return;
    }
    current_stats_path(stats_path, sizeof(stats_path));
    len = snprintf(buf, sizeof(buf),
                   "{\"schema\":\"runtime-lab-mmapfb-stats/v1\","
                   "\"pid\":%d,\"mode\":\"%s\",\"min_bytes\":%lu,"
                   "\"pool_size\":%llu,"
                   "\"intercepted\":%llu,\"redirected_count\":%llu,"
                   "\"redirected_bytes\":%llu,"
                   "\"active_count\":%llu,\"active_bytes\":%llu,"
                   "\"bypassed_total\":%llu,\"bypassed_bytes\":%llu,"
                   "\"backing_bytes_allocated\":%llu,"
                   "\"backing_file\":\"%s\","
                   "\"bypass_by_reason\":{",
                   (int)getpid(), g_shared_mode ? "shared" : "private",
                   (unsigned long)g_min_bytes,
                   (unsigned long long)g_pool_size,
                   g_stats.intercepted, g_stats.redirected_count,
                   g_stats.redirected_bytes, g_stats.active_count,
                   g_stats.active_bytes, g_stats.bypassed_total,
                   g_stats.bypassed_bytes,
                   g_stats.backing_bytes_allocated, g_pool_path);
    for (i = 0; i < N_REASONS; i++) {
        len += snprintf(buf + len, sizeof(buf) - (size_t)len,
                        "%s\"%s\":%llu", i ? "," : "", k_reasons[i],
                        g_stats.by_reason[i]);
        if (len >= (int)sizeof(buf) - 128)
            break;
    }
    if (len < (int)sizeof(buf) - 4) {
        buf[len++] = '}';
        buf[len++] = '}';
        buf[len++] = '\n';
    }
    pthread_mutex_unlock(&g_lock);

    fd = open(stats_path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd >= 0) {
        size_t off = 0;
        while (off < (size_t)len) {
            ssize_t w =
                write(fd, buf + off, (size_t)len - off);
            if (w <= 0)
                break;
            off += (size_t)w;
        }
        close(fd);
    }
    /* One-line stderr summary for log scraping (best effort). */
    {
        char line[512];
        int n = snprintf(line, sizeof(line),
                         "[mmapfb] intercepted=%llu redirected=%llu/%lluB "
                         "active=%llu/%lluB bypassed=%llu reason_small=%llu "
                         "backing=%lluB mode=%s\n",
                         g_stats.intercepted, g_stats.redirected_count,
                         g_stats.redirected_bytes, g_stats.active_count,
                         g_stats.active_bytes, g_stats.bypassed_total,
                         g_stats.by_reason[reason_index(REASON_TOO_SMALL)],
                         g_stats.backing_bytes_allocated,
                         g_shared_mode ? "shared" : "private");
        if (n > 0) {
            ssize_t w = write(STDERR_FILENO, line, (size_t)n);
            (void)w;
        }
    }
}

__attribute__((destructor)) static void mmapfb_fini(void) {
    /* Best-effort final flush; the periodic flusher is the primary path
     * because raw-exit runtimes skip destructors. A disabled passthrough
     * stays silent so default behaviour is unchanged. */
    write_stats_file();
}
