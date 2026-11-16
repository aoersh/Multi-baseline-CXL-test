/* Random dependent pointer-chase with per-access cycle timing.
 *
 * Purpose: measure every individual load latency (not a binned histogram),
 * so that sub-32-cycle events can be inspected directly and their timing
 * structure (periodicity, clustering) can be analysed.
 *
 * Memory placement and CPU affinity are set by the caller via numactl, so
 * this tool never needs privilege.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/mman.h>

#define PG 4096UL

static double now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec * 1e9 + (double)ts.tv_nsec;
}

static double calibrate_tsc(void)
{
    struct timespec req = {0, 100 * 1000 * 1000};
    double n0 = now_ns();
    uint64_t t0 = __builtin_ia32_rdtsc();
    nanosleep(&req, NULL);
    uint64_t t1 = __builtin_ia32_rdtsc();
    double n1 = now_ns();
    return (double)(t1 - t0) / (n1 - n0);
}

static void touch_numa_maps(void)
{
    FILE *f = fopen("/proc/self/numa_maps", "r");
    if (!f)
        return;
    char line[4096];
    while (fgets(line, sizeof line, f)) {
        if (strstr(line, "chase-buf") || strstr(line, "anon") || strstr(line, "stack")) {
            /* print first field and the per-node counters only */
            char *sp = strchr(line, ' ');
            if (!sp)
                continue;
            printf("# numa_maps:%.*s", (int)(sp - line), line);
            const char *p = sp;
            const char *keys[6] = {"N0=", "N1=", "N2=", "N3=", "huge=", "kernelpagesize_kB="};
            for (int k = 0; k < 6; k++) {
                const char *q = strstr(p, keys[k]);
                if (!q)
                    continue;
                const char *e = q;
                while (*e && *e != ' ' && *e != '\n')
                    e++;
                printf(" %.*s", (int)(e - q), q);
            }
            printf("\n");
        }
    }
    fclose(f);
}

struct opt {
    size_t size_mb;
    size_t stride;
    size_t samples;
    int trace;
    unsigned seed;
    const char *label;
};

int main(int argc, char **argv)
{
    struct opt o = {256, 64, 2000000, 0, 20261113u, "run"};
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--size-mb") && i + 1 < argc)
            o.size_mb = strtoul(argv[++i], NULL, 10);
        else if (!strcmp(argv[i], "--stride") && i + 1 < argc)
            o.stride = strtoul(argv[++i], NULL, 10);
        else if (!strcmp(argv[i], "--samples") && i + 1 < argc)
            o.samples = strtoul(argv[++i], NULL, 10);
        else if (!strcmp(argv[i], "--trace"))
            o.trace = 1;
        else if (!strcmp(argv[i], "--seed") && i + 1 < argc)
            o.seed = (unsigned)strtoul(argv[++i], NULL, 10);
        else if (!strcmp(argv[i], "--label") && i + 1 < argc)
            o.label = argv[++i];
        else {
            fprintf(stderr, "unknown/incomplete argument: %s\n", argv[i]);
            return 2;
        }
    }

    if (o.stride < 64 || o.stride > PG || (o.stride & (o.stride - 1))) {
        fprintf(stderr, "stride must be a power of two, 64..4096\n");
        return 2;
    }
    if (o.samples > 40000000UL)
        o.samples = 40000000UL;

    double tsc_ghz = calibrate_tsc();
    size_t bytes = o.size_mb << 20;
    uint8_t *base = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
                         MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE, -1, 0);
    if (base == MAP_FAILED) {
        perror("mmap");
        return 1;
    }

    size_t nodes = bytes / o.stride;
    size_t *perm = malloc(nodes * sizeof(size_t));
    uint32_t *lat = malloc(o.samples * sizeof(uint32_t));
    uint64_t *ts = o.trace ? malloc(o.samples * sizeof(uint64_t)) : NULL;
    if (!perm || !lat || (o.trace && !ts)) {
        fprintf(stderr, "allocation failed\n");
        return 1;
    }

    /* Deterministic shuffle: same chain for every stride so runs are comparable. */
    unsigned s = o.seed;
    for (size_t i = 0; i < nodes; i++)
        perm[i] = i;
    for (size_t i = nodes - 1; i > 0; i--) {
        s = s * 1664525u + 1013904223u;
        size_t j = (size_t)(((uint64_t)s << 32 | (s * 22695477u + 1u)) % (i + 1));
        size_t t = perm[i];
        perm[i] = perm[j];
        perm[j] = t;
    }

    /* INIT phase: fault in every page and link the chain. */
    double init0 = now_ns();
    for (size_t i = 0; i < nodes; i++) {
        size_t cur = perm[i] * o.stride;
        size_t nxt = perm[(i + 1) % nodes] * o.stride;
        *(uint64_t *)(base + cur) = (uint64_t)(base + nxt);
        base[cur + 8] = (uint8_t)i; /* second touch is harmless, keeps page real */
    }
    double init1 = now_ns();
    printf("# label %s size_mb %zu stride %zu nodes %zu tsc_ghz %.6f\n",
           o.label, o.size_mb, o.stride, nodes, tsc_ghz);
    printf("# init_seconds %.3f\n", (init1 - init0) / 1e9);
    touch_numa_maps();

    /* Warm the chain once: this is included in no statistic. */
    {
        volatile uint64_t *p = (volatile uint64_t *)base;
        for (size_t i = 0; i < nodes; i++)
            p = (volatile uint64_t *)(*p);
    }

    double m0 = now_ns();
    volatile uint64_t *p = (volatile uint64_t *)base;
    for (size_t i = 0; i < o.samples; i++) {
        unsigned aux;
        uint64_t a = __builtin_ia32_rdtscp(&aux);
        p = (volatile uint64_t *)(*p);
        uint64_t b = __builtin_ia32_rdtscp(&aux);
        lat[i] = (uint32_t)(b - a);
        if (ts)
            ts[i] = b;
    }
    double m1 = now_ns();
    printf("# measure_seconds %.3f access_per_second %.0f\n", (m1 - m0) / 1e9,
           o.samples / ((m1 - m0) / 1e9));

    /* Streaming summary to stdout, full samples to a .bin next to it. */
    uint32_t maxc = 0;
    uint64_t sum = 0, fast = 0;
    for (size_t i = 0; i < o.samples; i++) {
        if (lat[i] > maxc)
            maxc = lat[i];
        sum += lat[i];
        if (lat[i] < 32)
            fast++;
    }
    printf("# summary n %zu mean_cycles %.2f mean_ns %.2f sub32_pct %.4f max_cycles %u\n",
           o.samples, (double)sum / (double)o.samples,
           (double)sum / (double)o.samples / tsc_ghz, 100.0 * (double)fast / (double)o.samples,
           maxc);

    char path[4096];
    snprintf(path, sizeof path, "%s.lat.bin", o.label);
    FILE *lf = fopen(path, "wb");
    if (lf) {
        fwrite(lat, sizeof(uint32_t), o.samples, lf);
        fclose(lf);
    }
    if (ts) {
        snprintf(path, sizeof path, "%s.tsc.bin", o.label);
        FILE *tf = fopen(path, "wb");
        if (tf) {
            fwrite(ts, sizeof(uint64_t), o.samples, tf);
            fclose(tf);
        }
    }
    free(perm);
    free(lat);
    free(ts);
    munmap(base, bytes);
    return 0;
}
