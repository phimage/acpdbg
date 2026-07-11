/*
 * A tiny program that crashes, for trying out acpdbg.
 *
 *   make            # build ./crash with debug info (-g)
 *   acpdbg -- ./crash
 *
 * Run with no arguments and `name` is NULL, so strlen() dereferences a NULL
 * pointer inside describe() and the program receives SIGSEGV.
 */
#include <stdio.h>
#include <string.h>

static size_t describe(const char *label, const char *s) {
    printf("describing %s...\n", label);
    return strlen(s); /* crashes here when s == NULL */
}

int main(int argc, char **argv) {
    const char *name = (argc > 1) ? argv[1] : NULL;
    size_t len = describe("name", name);
    printf("length of name = %zu\n", len);
    return 0;
}
