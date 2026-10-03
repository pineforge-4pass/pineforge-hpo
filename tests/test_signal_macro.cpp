#include <signal.h>

#undef sigemptyset
#define sigemptyset(mask) (((*(mask)) = sigset_t{}), 0)

#include "../src/cli/main.cpp"
