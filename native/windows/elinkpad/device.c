/* Explicit test-device lifetime helper; never installs a driver or certificate. */
#define _WIN32_WINNT 0x0A00
#include <windows.h>
#include <swdevice.h>
#include <objbase.h>
#include <stdio.h>
#include "protocol.h"

static HANDLE created;
static HRESULT result = E_PENDING;
static VOID WINAPI ready(HSWDEVICE device, HRESULT status, PVOID context, PCWSTR instance) {
    (void)device; (void)context;
    result = status;
    if (SUCCEEDED(status)) wprintf(L"Device: %ls\n", instance);
    SetEvent(created);
}
int wmain(int argc, wchar_t **argv) {
    GUID container;
    wchar_t instance[64];
    HSWDEVICE device = NULL;
    SW_DEVICE_CREATE_INFO info = {0};
    HRESULT hr;
    if (argc != 2 || wcscmp(argv[1], L"--hold")) {
        puts("In an isolated driver test OS, stage a signed INF first, then run elevated:\n"
             "  ElinkPadDevice.exe --hold\n"
             "Keep this console open. Enter or process exit removes this temporary device.\n"
             "This tool does not install drivers, trust certificates or change boot settings.");
        return 2;
    }
    hr = CoCreateGuid(&container);
    if (FAILED(hr)) return 1;
    StringFromGUID2(&container, instance, 64);
    created = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (!created) return 1;
    info.cbSize = sizeof(info);
    info.pszInstanceId = instance;
    info.pszzHardwareIds = EP_HARDWARE_ID L"\0";
    info.pszDeviceDescription = L"Elink Virtual Xbox Pad (Experimental)";
    info.pContainerId = &container;
    info.CapabilityFlags = SWDeviceCapabilitiesDriverRequired | SWDeviceCapabilitiesRemovable;
    hr = SwDeviceCreate(L"ElinkPad", L"HTREE\\ROOT\\0", &info, 0, NULL, ready, NULL, &device);
    if (SUCCEEDED(hr)) {
        /* Default SWDeviceLifetimeHandle removes the device even on process death. */
        if (WaitForSingleObject(created, 15000) == WAIT_OBJECT_0) hr = result;
        else hr = HRESULT_FROM_WIN32(ERROR_TIMEOUT);
        if (SUCCEEDED(hr)) { puts("Temporary devnode created; driver binding/XInput still require validation. Enter to remove."); getchar(); }
        SwDeviceClose(device);
    }
    if (FAILED(hr)) fprintf(stderr, "SwDeviceCreate failed: 0x%08lx (elevation and staged driver may be required).\n", (unsigned long)hr);
    CloseHandle(created);
    return FAILED(hr) ? 1 : 0;
}
