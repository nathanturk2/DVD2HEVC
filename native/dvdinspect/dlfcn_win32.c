#ifdef _WIN32
#include <windows.h>
#include <stdio.h>

void *dlopen(const char *file, int mode) {
    (void)mode;
    return (void *)LoadLibraryA(file);
}

int dlclose(void *handle) {
    return FreeLibrary((HMODULE)handle) ? 0 : -1;
}

void *dlsym(void *handle, const char *name) {
    return (void *)GetProcAddress((HMODULE)handle, name);
}

char *dlerror(void) {
    static char message[64];
    snprintf(message, sizeof(message), "Win32 loader error %lu", GetLastError());
    return message;
}
#endif
