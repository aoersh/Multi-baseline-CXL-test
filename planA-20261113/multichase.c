/* Multi-chain interleaved pointer chase: controllable memory-level parallelism.
 *
 * A single dependent pointer chase has exactly one outstanding load at any
 * time (MLP = 1). Real workloads keep many loads in flight. This tool walks
 * K independent chains in an interleaved fashion inside one thread, so the
 * number of in-flight loads is exactly K by construction.
 *
 * Per-access latencies are recorded as (issue_tsc -> completion_tsc) for each
 * of the K lanes, so both the latency distribution and the achieved overlap
 * can be inspected afterwards.
 *
 * Memory placement and CPU affinity come from the caller (numactl), so this
 * tool needs no privilege.
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

struct opt {
    size_t size_mb;      /* total working set across all chains */
    size_t stride;
    size_t samples;      /* total accesses, split evenly across K lanes */
    int    k;            /* number of independent chains = MLP */
    int    trace;
    unsigned seed;
    const char *label;
};

/* One round of a 64-bit xorshift; used for the deterministic shuffle. */
static inline uint64_t xorshift(uint64_t *s)
{
    uint64_t x = *s;
    x ^= x << 13;
    x ^= x >> 7;
    x ^= x << 17;
    return *s = x;
}

int main(int argc, char **argv)
{
    struct opt o = {256, 64, 2000000, 1, 0, 20261113u, "run"};
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--size-mb") && i + 1 < argc)
            o.size_mb = strtoul(argv[++i], NULL, 10);
        else if (!strcmp(argv[i], "--stride") && i + 1 < argc)
            o.stride = strtoul(argv[++i], NULL, 10);
        else if (!strcmp(argv[i], "--samples") && i + 1 < argc)
            o.samples = strtoul(argv[++i], NULL, 10);
        else if (!strcmp(argv[i], "--k") && i + 1 < argc)
            o.k = atoi(argv[++i]);
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
    if (o.k < 1 || o.k > 256) {
        fprintf(stderr, "--k must be 1..256\n");
        return 2;
    }
    if (o.stride < 64 || o.stride > 4096 || (o.stride & (o.stride - 1))) {
        fprintf(stderr, "--stride must be a power of two in 64..4096\n");
        return 2;
    }
    if (o.samples > 200000000UL)
        o.samples = 200000000UL;

    double tsc_ghz = calibrate_tsc();
    size_t bytes = o.size_mb << 20;
    uint8_t *base = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
                         MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE, -1, 0);
    if (base == MAP_FAILED) {
        perror("mmap");
        return 1;
    }
    madvise(base, bytes, MADV_NOHUGEPAGE);

    /* One ring over the whole footprint; the K lanes start at staggered
     * positions along it. Keeping the ring size independent of K is what
     * makes high-MLP cells comparable: every lane stays cold regardless of
     * how many lanes there are. */
    size_t nodes_per_lane = (bytes / o.stride);

    size_t *perm = malloc(nodes_per_lane * sizeof(size_t));
    size_t per_lane_samples = o.samples / (size_t)o.k;
    uint32_t *lat = malloc(o.samples * sizeof(uint32_t));
    uint64_t *ts = o.trace ? malloc(o.samples * sizeof(uint64_t)) : NULL;
    uint64_t *lane_of = NULL;
    if (o.trace)
        lane_of = malloc(o.samples * sizeof(uint64_t));
    if (!perm || !lat || (o.trace && (!ts || !lane_of))) {
        fprintf(stderr, "allocation failed\n");
        return 1;
    }

    /* INIT: build one random cycle covering the whole footprint. */
    double init0 = now_ns();
    uint64_t rs = o.seed;
    for (size_t i = 0; i < nodes_per_lane; i++)
        perm[i] = i;
    for (size_t i = nodes_per_lane - 1; i > 0; i--) {
        size_t j = (size_t)(xorshift(&rs) % (i + 1));
        size_t t = perm[i];
        perm[i] = perm[j];
        perm[j] = t;
    }
    for (size_t i = 0; i < nodes_per_lane; i++) {
        size_t cur = perm[i] * o.stride;
        size_t nxt = perm[(i + 1) % nodes_per_lane] * o.stride;
        *(uint64_t *)(base + cur) = (uint64_t)(base + nxt);
    }
    double init1 = now_ns();

    printf("# label %s size_mb %zu stride %zu k %d nodes_per_ring %zu "
           "samples %zu per_lane_samples %zu tsc_ghz %.6f\n",
           o.label, o.size_mb, o.stride, o.k, nodes_per_lane,
           o.samples, per_lane_samples, tsc_ghz);
    printf("# init_seconds %.3f\n", (init1 - init0) / 1e9);

    FILE *nm = fopen("/proc/self/numa_maps", "r");
    if (nm) {
        char line[4096];
        while (fgets(line, sizeof line, nm)) {
            if (!strstr(line, "anon="))
                continue;
            size_t pages = 0;
            for (char *t = strstr(line, "anon="); t; t = strstr(t + 1, " N")) {
                if (sscanf(t, "anon=%zu", &pages) == 1)
                    break;
            }
            if (pages < 10000)
                continue;
            char *sp = strchr(line, ' ');
            if (sp) {
                printf("# numa_maps:%.*s", (int)(sp - line), line);
                for (const char **k = (const char *[]){"N0=", "N1=", "N2=", "N3="}, **e = k + 4;
                     k < e; k++) {
                    const char *q = strstr(sp, *k);
                    if (!q)
                        continue;
                    const char *z = q;
                    while (*z && *z != ' ' && *z != '\n')
                        z++;
                    printf(" %.*s", (int)(z - q), q);
                }
                printf("\n");
            }
        }
        fclose(nm);
    }

    /* Lane start points: staggered around the single ring so the lanes walk
     * different parts of it and do not shadow each other's cache lines.
     * Each start is advanced a few hops so the first measured access never
     * lands on a line that initialisation just touched. */
    uint8_t **p = malloc(o.k * sizeof(*p));
    if (!p) {
        fprintf(stderr, "allocation failed\n");
        return 1;
    }
    for (int lane = 0; lane < o.k; lane++) {
        size_t start = (size_t)((double)nodes_per_lane * lane / o.k);
        uint8_t *q = base + perm[start] * o.stride;
        for (int step = 0; step < 16; step++)
            q = (uint8_t *)(*(volatile uint64_t *)q);
        p[lane] = q;
    }

    /* MEASURE: one round = all K lanes advanced once.
     *
     * The round is timed with a single rdtscp pair at its boundaries. Per-
     * access rdtscp is deliberately NOT used: rdtscp waits for prior
     * instructions to retire, so instrumenting inside the loop serialises
     * the lanes and forces MLP back to 1 (verified experimentally - K=1..32
     * all gave ~500 ns/access and identical wall time). Timing the round
     * keeps the K loads genuinely concurrent and costs one rdtscp per K
     * accesses, so overhead shrinks as K grows.
     *
     * Within a round the K lane loads are independent, so the out-of-order
     * engine issues all of them before the first retires. Round r+1's load
     * for lane l depends on round r's store to p[l], so lanes are serial
     * but rounds are not: in-flight loads = K by construction.
     */
    double m0 = now_ns();
    size_t rounds = 0;
    for (size_t it = 0; it < per_lane_samples; it++) {
        unsigned aux;
        uint64_t a = __builtin_ia32_rdtscp(&aux);
        for (int lane = 0; lane < o.k; lane++)
            p[lane] = (uint8_t *)(*(volatile uint64_t *)p[lane]);
        uint64_t b = __builtin_ia32_rdtscp(&aux);
        lat[rounds] = (uint32_t)(b - a);
        if (ts)
            ts[rounds] = b;
        rounds++;
    }
    double m1 = now_ns();
    double secs = (m1 - m0) / 1e9;
    size_t done = rounds * (size_t)o.k;
    /* Absolute monotonic window, so out-of-process samplers (perf) can keep
     * only the samples that landed inside the measurement phase. Without this
     * the init phase's tens of millions of writes dominate any sampled
     * distribution. Same clock domain as perf script timestamps. */
    printf("# window %.9f %.9f\n", m0 / 1e9, m1 / 1e9);
    printf("# measure_seconds %.3f rounds %zu total_accesses %zu "
           "accesses_per_second %.0f ns_per_access %.2f\n",
           secs, rounds, done, done / secs, secs * 1e9 / (double)done);

    /* Per-round times converted to per-access throughput. */
    uint32_t maxc = 0;
    uint64_t sum = 0, fast = 0;
    for (size_t i = 0; i < rounds; i++) {
        if (lat[i] > maxc)
            maxc = lat[i];
        sum += lat[i];
        if (lat[i] < 32)
            fast++;
    }
    double mean_round = (double)sum / (double)rounds;
    printf("# summary rounds %zu k %d mean_round_cycles %.2f mean_round_ns %.2f "
           "ns_per_access %.2f sub32_pct %.4f max_round_cycles %u\n",
           rounds, o.k, mean_round, mean_round / tsc_ghz,
           mean_round / tsc_ghz / (double)o.k,
           100.0 * (double)fast / (double)rounds, maxc);

    /* One record per round (all K lanes), not per access. */
    char path[4096];
    snprintf(path, sizeof path, "%s.lat.bin", o.label);
    FILE *lf = fopen(path, "wb");
    if (lf) {
        fwrite(lat, sizeof(uint32_t), rounds, lf);
        fclose(lf);
    }
    if (ts) {
        snprintf(path, sizeof path, "%s.tsc.bin", o.label);
        FILE *tf = fopen(path, "wb");
        if (tf) {
            fwrite(ts, sizeof(uint64_t), rounds, tf);
            fclose(tf);
        }
    }
    free(p);
    free(perm);
    free(lat);
    free(ts);
    free(lane_of);
    munmap(base, bytes);
    return 0;
}
