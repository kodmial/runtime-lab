/* Minimal file-backed malloc interposer for measurement only (issue #56).
 *
 * Purpose: quantify how much of an unmodified binary's memory travels
 * through interposable libc allocation paths by backing those allocations
 * with MAP_SHARED file mappings on ordinary (non-DAX) disk. This is a
 * benchmark probe, NOT a production allocator: it has a fixed-size pool,
 * a simple first-fit free list, and no tuning. Do not ship it.
 *
 * Build: gcc -O2 -shared -fPIC -o libdiskheap_shim.so shim.c -ldl -lpthread
 *
 * Environment:
 *   DISKHEAP_DIR      directory for the backing file (required)
 *   DISKHEAP_SIZE_MB  pool size in MB (default 1024)
 *
 * Interposed: malloc, calloc, realloc, free, posix_memalign, memalign,
 * aligned_alloc, malloc_usable_size. Pointers not owned by the pool are
 * forwarded to the real libc implementations, so mixed direct-mmap heaps
 * (Bun/JSC) keep working and only libc-malloc traffic moves to disk.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#define SHIM_MAGIC 0x64687368696d2141ULL /* "dhshim!A" */
#define SHIM_ALIGN 16

typedef struct block {
    uint64_t magic;
    size_t size;            /* payload bytes */
    int free;
    struct block *next;     /* free-list link (valid only when free) */
    void *base;             /* pool base this block belongs to (== pool) */
} block_t;

static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static __thread int in_shim = 0;
static char *pool = NULL;
static size_t pool_size = 0;
static size_t pool_used = 0;
static block_t *free_list = NULL;

/* Emergency static buffer for allocations re-entered during init. */
static char emergency[65536];
static size_t emergency_used = 0;

static void *(*real_malloc)(size_t) = NULL;
static void (*real_free)(void *) = NULL;
static void *(*real_calloc)(size_t, size_t) = NULL;
static void *(*real_realloc)(void *, size_t) = NULL;

static size_t align_up(size_t n, size_t a) { return (n + a - 1) & ~(a - 1); }

static void ensure_real(void) {
    if (real_malloc)
        return;
    real_malloc = dlsym(RTLD_NEXT, "malloc");
    real_free = dlsym(RTLD_NEXT, "free");
    real_calloc = dlsym(RTLD_NEXT, "calloc");
    real_realloc = dlsym(RTLD_NEXT, "realloc");
}

/* Pool init state: 0 = uninit, 1 = ready, -1 = unavailable.
 * init_once() runs with the caller's in_shim guard held, so any libc
 * allocation re-entered from dlsym lands in the emergency buffer. */
static int pool_state = 0;

static void init_once(void) {
    char path[4096];
    const char *dir;
    long mbs;
    int fd;
    size_t size;
    char *addr;

    if (pool_state != 0)
        return;
    ensure_real();
    dir = getenv("DISKHEAP_DIR");
    if (!dir || !*dir) {
        pool_state = -1;
        return;
    }
    mbs = 1024;
    {
        const char *s = getenv("DISKHEAP_SIZE_MB");
        if (s && *s)
            mbs = atol(s);
    }
    if (mbs < 16)
        mbs = 16;
    if (mbs > 8192)
        mbs = 8192;
    size = (size_t)mbs * 1024 * 1024;
    snprintf(path, sizeof(path), "%s/diskheap.XXXXXX", dir);
    fd = mkstemp(path);
    if (fd < 0) {
        pool_state = -1;
        return;
    }
    unlink(path);
    if (ftruncate(fd, (off_t)size) != 0) {
        close(fd);
        pool_state = -1;
        return;
    }
    addr = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    close(fd);
    if (addr == MAP_FAILED) {
        pool_state = -1;
        return;
    }
    pool = addr;
    pool_size = size;
    pool_used = 0;
    free_list = NULL;
    pool_state = 1;
}

/* Caller must hold lock. Every payload is preceded directly by its header,
 * so free() needs no side tables: hdr = payload - sizeof(block_t). */
static void *pool_alloc_locked(size_t size, size_t align) {
    block_t **prev;
    block_t *b;
    char *hdr_at;
    char *payload;

    if (align < SHIM_ALIGN)
        align = SHIM_ALIGN;
    if (align > 4096) {
        /* mmap Hands us page-aligned memory; larger alignments are rare
         * (and never observed in this benchmark) so decline them here and
         * let the caller fall back to libc instead of corrupting state. */
        errno = EINVAL;
        return NULL;
    }
    size = align_up(size ? size : 1, SHIM_ALIGN);
    for (prev = &free_list; (b = *prev) != NULL; prev = &b->next) {
        char *cand = (char *)b + sizeof(block_t);
        if (b->free && b->size >= size &&
            ((uintptr_t)cand & (align - 1)) == 0) {
            *prev = b->next;
            b->free = 0;
            b->next = NULL;
            return cand;
        }
    }
    hdr_at = (char *)align_up(
        (uintptr_t)(pool + pool_used) + sizeof(block_t), align) -
        sizeof(block_t);
    payload = hdr_at + sizeof(block_t);
    if ((size_t)(payload + size - pool) > pool_size) {
        errno = ENOMEM;
        return NULL;
    }
    pool_used = (size_t)(payload + size - pool);
    b = (block_t *)hdr_at;
    b->magic = SHIM_MAGIC;
    b->size = size;
    b->free = 0;
    b->next = NULL;
    b->base = pool;
    return payload;
}

/* Caller must hold lock. Coalescing is best-effort: adjacent free blocks
 * merge only when the freed block physically precedes a free-list block. */
static void pool_free_locked(void *ptr) {
    block_t *b = (block_t *)((char *)ptr - sizeof(block_t));
    block_t *o;
    if (b->magic != SHIM_MAGIC || b->base != pool)
        return;
    b->free = 1;
    for (o = free_list; o; o = o->next) {
        if ((char *)b + sizeof(block_t) + b->size == (char *)o && o->free) {
            b->size += sizeof(block_t) + o->size;
            /* unlink o */
            {
                block_t **pp;
                for (pp = &free_list; *pp; pp = &(*pp)->next) {
                    if (*pp == o) {
                        *pp = o->next;
                        break;
                    }
                }
            }
            break;
        }
    }
    b->next = free_list;
    free_list = b;
}

void *malloc(size_t size) {
    void *p;
    if (in_shim) {
        if (emergency_used + size <= sizeof(emergency)) {
            p = emergency + emergency_used;
            emergency_used += align_up(size ? size : 1, 16);
            return p;
        }
        return NULL;
    }
    in_shim = 1;
    init_once();
    pthread_mutex_lock(&lock);
    p = (pool_state == 1) ? pool_alloc_locked(size, SHIM_ALIGN) : NULL;
    pthread_mutex_unlock(&lock);
    if (!p) {
        ensure_real();
        p = real_malloc ? real_malloc(size) : NULL;
    }
    in_shim = 0;
    return p;
}

void free(void *ptr) {
    if (!ptr)
        return;
    if (in_shim)
        return; /* emergency buffer is never reclaimed */
    in_shim = 1;
    pthread_mutex_lock(&lock);
    {
        block_t *b = (block_t *)((char *)ptr - sizeof(block_t));
        if (pool && (char *)b >= pool && (char *)b < pool + pool_size &&
            b->magic == SHIM_MAGIC && b->base == pool && !b->free) {
            pool_free_locked(ptr);
            pthread_mutex_unlock(&lock);
            in_shim = 0;
            return;
        }
    }
    pthread_mutex_unlock(&lock);
    ensure_real();
    if (real_free)
        real_free(ptr);
    in_shim = 0;
}

void *calloc(size_t nmemb, size_t size) {
    size_t total;
    void *p;
    if (nmemb && size > (size_t)-1 / nmemb) {
        errno = ENOMEM;
        return NULL;
    }
    total = nmemb * size;
    p = malloc(total);
    if (p)
        memset(p, 0, total);
    return p;
}

void *realloc(void *ptr, size_t size) {
    void *n;
    size_t old = 0;
    if (!ptr)
        return malloc(size);
    if (size == 0) {
        free(ptr);
        return NULL;
    }
    pthread_mutex_lock(&lock);
    {
        block_t *b = (block_t *)((char *)ptr - sizeof(block_t));
        if (pool && (char *)b >= pool && (char *)b < pool + pool_size &&
            b->magic == SHIM_MAGIC && b->base == pool && !b->free)
            old = b->size;
    }
    pthread_mutex_unlock(&lock);
    if (!old) {
        /* foreign pointer: forward to libc */
        void *r;
        if (in_shim)
            return NULL;
        in_shim = 1;
        ensure_real();
        r = real_realloc ? real_realloc(ptr, size) : NULL;
        in_shim = 0;
        return r;
    }
    n = malloc(size);
    if (!n)
        return NULL;
    memcpy(n, ptr, old < size ? old : size);
    free(ptr);
    return n;
}

int posix_memalign(void **memptr, size_t alignment, size_t size) {
    void *raw = NULL;
    size_t align = alignment;
    /* memptr is declared nonnull by libc; callers must pass a valid slot. */
    if (align < sizeof(void *))
        align = sizeof(void *);
    if (align & (align - 1)) {
        size_t a = sizeof(void *);
        while (a < align)
            a <<= 1;
        align = a;
    }
    if (in_shim)
        return ENOMEM;
    in_shim = 1;
    init_once();
    pthread_mutex_lock(&lock);
    if (pool_state == 1)
        raw = pool_alloc_locked(size, align);
    pthread_mutex_unlock(&lock);
    if (!raw) {
        ensure_real();
        if (real_malloc && (raw = real_malloc(size + align)) != NULL) {
            uintptr_t a = ((uintptr_t)raw + align - 1) & ~(uintptr_t)(align - 1);
            /* libc fallback cannot serve true alignment without a header;
             * only accept it when already aligned (common for 16/32). */
            if (a != (uintptr_t)raw) {
                real_free(raw);
                raw = NULL;
            }
        }
        if (!raw) {
            in_shim = 0;
            return ENOMEM;
        }
    }
    *memptr = raw;
    in_shim = 0;
    return 0;
}

void *memalign(size_t alignment, size_t size) {
    void *p = NULL;
    if (posix_memalign(&p, alignment, size) != 0)
        return NULL;
    return p;
}

void *aligned_alloc(size_t alignment, size_t size) {
    void *p = NULL;
    if (posix_memalign(&p, alignment, size) != 0)
        return NULL;
    return p;
}

size_t malloc_usable_size(void *ptr) {
    size_t s = 0;
    if (!ptr)
        return 0;
    pthread_mutex_lock(&lock);
    {
        block_t *b = (block_t *)((char *)ptr - sizeof(block_t));
        if (pool && (char *)b >= pool && (char *)b < pool + pool_size &&
            b->magic == SHIM_MAGIC && b->base == pool && !b->free)
            s = b->size;
    }
    pthread_mutex_unlock(&lock);
    return s;
}
