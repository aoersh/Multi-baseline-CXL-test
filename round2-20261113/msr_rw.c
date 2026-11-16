/* Minimal rdmsr/wrmsr equivalent: pread/pwrite on /dev/cpu/<n>/msr.
 *
 * The msr driver exposes one 8-byte register per lseek offset, so a plain
 * pread/pwrite at offset (msr_index * 8) is exactly what rdmsr/wrmsr do.
 * Requires root; used only by prefetch_toggle.sh.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv)
{
    if (argc != 4 || (strcmp(argv[1], "read") && strcmp(argv[1], "write"))) {
        fprintf(stderr, "usage: %s read|write <cpu> <msr>\n"
                        "       write reads the value from stdin\n", argv[0]);
        return 2;
    }
    int cpu = atoi(argv[2]);
    unsigned long long msr = strtoull(argv[3], NULL, 0);
    char path[64];
    snprintf(path, sizeof path, "/dev/cpu/%d/msr", cpu);

    int fd = open(path, O_RDWR);
    if (fd < 0) {
        fprintf(stderr, "open %s: %s\n", path, strerror(errno));
        return 1;
    }
    off_t off = (off_t)msr;
    if (!strcmp(argv[1], "read")) {
        uint64_t v = 0;
        if (pread(fd, &v, sizeof v, off) != (ssize_t)sizeof v) {
            fprintf(stderr, "pread %s 0x%llx: %s\n", path, msr, strerror(errno));
            close(fd);
            return 1;
        }
        printf("0x%016llx\n", (unsigned long long)v);
    } else {
        unsigned long long v;
        if (scanf("%llx", &v) != 1) {
            fprintf(stderr, "expected a hex value on stdin\n");
            close(fd);
            return 2;
        }
        if (pwrite(fd, &v, sizeof v, off) != (ssize_t)sizeof v) {
            fprintf(stderr, "pwrite %s 0x%llx: %s\n", path, msr, strerror(errno));
            close(fd);
            return 1;
        }
    }
    close(fd);
    return 0;
}
