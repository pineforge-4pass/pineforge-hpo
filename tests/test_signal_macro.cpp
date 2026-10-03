#include <signal.h>

#define sigemptyset(mask) (((*(mask)) = sigset_t{}), 0)

#include "../src/cli/main.cpp"
