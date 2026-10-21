/* Elink experimental UMDF2 XUSB driver. See README and third-party notices. */
#define WIN32_NO_STATUS
#include <windows.h>
#undef WIN32_NO_STATUS
#include <ntstatus.h>
#include <wdf.h>
#include "protocol.h"

DRIVER_INITIALIZE DriverEntry;
EVT_WDF_DRIVER_DEVICE_ADD EpDeviceAdd;
EVT_WDF_IO_QUEUE_IO_DEVICE_CONTROL EpIoControl;
EVT_WDF_DEVICE_FILE_CREATE EpFileCreate;
EVT_WDF_FILE_CLEANUP EpFileCleanup;
EVT_WDF_TIMER EpTimer;

typedef struct { EP_STATE state; WDFQUEUE waiting; } EP_CONTEXT;
WDF_DECLARE_CONTEXT_TYPE_WITH_NAME(EP_CONTEXT, EpContext)
static const GUID control_guid = EP_CONTROL_GUID_INIT;
static const GUID xusb_guid = EP_XUSB_GUID_INIT;

static void reply(WDFREQUEST request, const void *data, size_t size) {
    void *out;
    NTSTATUS status = WdfRequestRetrieveOutputBuffer(request, size, &out, NULL);
    if (NT_SUCCESS(status)) {
        memcpy(out, data, size);
        WdfRequestCompleteWithInformation(request, STATUS_SUCCESS, size);
    } else WdfRequestComplete(request, status);
}
static void pump(EP_CONTEXT *ctx) {
    WDFREQUEST request;
    uint8_t state[29];
    if (NT_SUCCESS(WdfIoQueueRetrieveNextRequest(ctx->waiting, &request))) {
        ep_xusb_state(&ctx->state, state, 1);
        reply(request, state, sizeof(state));
    }
}
VOID EpTimer(WDFTIMER timer) {
    EP_CONTEXT *ctx = EpContext((WDFDEVICE)WdfTimerGetParentObject(timer));
    ep_expire(&ctx->state, GetTickCount64());
    pump(ctx);
}
VOID EpFileCreate(WDFDEVICE device, WDFREQUEST request, WDFFILEOBJECT file) {
    UNREFERENCED_PARAMETER(device); UNREFERENCED_PARAMETER(file);
    WdfRequestComplete(request, STATUS_SUCCESS);
}
VOID EpFileCleanup(WDFFILEOBJECT file) {
    EP_CONTEXT *ctx = EpContext(WdfFileObjectGetDevice(file));
    ep_release(&ctx->state, (uintptr_t)file);
}
NTSTATUS DriverEntry(PDRIVER_OBJECT object, PUNICODE_STRING path) {
    WDF_DRIVER_CONFIG config;
    WDF_DRIVER_CONFIG_INIT(&config, EpDeviceAdd);
    return WdfDriverCreate(object, path, WDF_NO_OBJECT_ATTRIBUTES, &config, WDF_NO_HANDLE);
}
NTSTATUS EpDeviceAdd(WDFDRIVER driver, PWDFDEVICE_INIT init) {
    WDFDEVICE device;
    WDF_OBJECT_ATTRIBUTES attributes;
    WDF_FILEOBJECT_CONFIG files;
    WDF_IO_QUEUE_CONFIG queue;
    WDF_TIMER_CONFIG timer_config;
    WDFTIMER timer;
    NTSTATUS status;
    UNREFERENCED_PARAMETER(driver);
    WDF_FILEOBJECT_CONFIG_INIT(&files, EpFileCreate, WDF_NO_EVENT_CALLBACK, EpFileCleanup);
    WdfDeviceInitSetFileObjectConfig(init, &files, WDF_NO_OBJECT_ATTRIBUTES);
    WDF_OBJECT_ATTRIBUTES_INIT_CONTEXT_TYPE(&attributes, EP_CONTEXT);
    /* All state callbacks, including file cleanup and timer, share the device lock. */
    attributes.SynchronizationScope = WdfSynchronizationScopeDevice;
    attributes.ExecutionLevel = WdfExecutionLevelPassive;
    status = WdfDeviceCreate(&init, &attributes, &device);
    if (!NT_SUCCESS(status)) return status;
    WDF_IO_QUEUE_CONFIG_INIT_DEFAULT_QUEUE(&queue, WdfIoQueueDispatchParallel);
    queue.EvtIoDeviceControl = EpIoControl;
    status = WdfIoQueueCreate(device, &queue, WDF_NO_OBJECT_ATTRIBUTES, NULL);
    if (!NT_SUCCESS(status)) return status;
    WDF_IO_QUEUE_CONFIG_INIT(&queue, WdfIoQueueDispatchManual);
    status = WdfIoQueueCreate(device, &queue, WDF_NO_OBJECT_ATTRIBUTES, &EpContext(device)->waiting);
    if (!NT_SUCCESS(status)) return status;
    WDF_TIMER_CONFIG_INIT_PERIODIC(&timer_config, EpTimer, 8);
    WDF_OBJECT_ATTRIBUTES_INIT(&attributes);
    attributes.ParentObject = device;
    status = WdfTimerCreate(&timer_config, &attributes, &timer);
    if (!NT_SUCCESS(status)) return status;
    status = WdfDeviceCreateDeviceInterface(device, &control_guid, NULL);
    if (!NT_SUCCESS(status)) return status;
    status = WdfDeviceCreateDeviceInterface(device, &xusb_guid, NULL);
    if (!NT_SUCCESS(status)) return status;
    WdfTimerStart(timer, WDF_REL_TIMEOUT_IN_MS(8));
    return STATUS_SUCCESS;
}
VOID EpIoControl(WDFQUEUE queue, WDFREQUEST request, size_t out_length, size_t in_length, ULONG code) {
    EP_CONTEXT *ctx = EpContext(WdfIoQueueGetDevice(queue));
    EP_STATE *s = &ctx->state;
    uintptr_t owner = (uintptr_t)WdfRequestGetFileObject(request);
    uint8_t *input = NULL;
    NTSTATUS status = STATUS_INVALID_DEVICE_REQUEST;
    if (in_length) {
        status = WdfRequestRetrieveInputBuffer(request, in_length, (void **)&input, NULL);
        if (!NT_SUCCESS(status)) { WdfRequestComplete(request, status); return; }
    }
    ep_expire(s, GetTickCount64());
    switch (code) {
    case EP_QUERY: {
        EP_INFO info = {{EP_VERSION, sizeof(EP_INFO)}, 1, EP_TIMEOUT_MS};
        if (in_length) { status = STATUS_INVALID_PARAMETER; break; }
        reply(request, &info, sizeof(info)); return;
    }
    case EP_CLAIM:
        if (!ep_header(input, in_length, sizeof(EP_HEADER))) { status = STATUS_INVALID_PARAMETER; break; }
        status = ep_claim(s, owner) ? STATUS_SUCCESS : STATUS_SHARING_VIOLATION; break;
    case EP_UPDATE:
        status = ep_update(s, owner, input, in_length, GetTickCount64()) ? STATUS_SUCCESS : STATUS_INVALID_PARAMETER;
        if (NT_SUCCESS(status)) pump(ctx);
        break;
    case EP_POLL:
        if (in_length || !owner || owner != s->owner) { status = STATUS_ACCESS_DENIED; break; }
        reply(request, &s->feedback, sizeof(s->feedback)); return;
    case EP_RELEASE:
        if (!ep_header(input, in_length, sizeof(EP_HEADER)) || !owner || owner != s->owner) {
            status = STATUS_ACCESS_DENIED; break;
        }
        ep_release(s, owner); status = STATUS_SUCCESS; break;
    case 0x80006000: {
        const uint8_t info[12] = {3,1,1,0,0,0,0,0,0x5e,4,0x8e,2};
        reply(request, info, sizeof(info)); return;
    }
    case 0x8000e004: {
        /* Wire arrays adapted from HIDMaestro; retain its full MIT notice. */
        const uint8_t v1[24] = {3,1,0,1,0xff,0xf7,0xff,0xff,0xc0,0xff,0xc0,0xff,
            0xc0,0xff,0xc0,0xff,0xff,0xff,0xff,0xff,0,0,0xff,0xff};
        const uint8_t v2[36] = {3,1,1,1,0x0c,0,0x5e,4,0x8e,2,0x10,1,0,0xfa,0x34,0x22,
            0xff,0xf7,0xff,0xff,0xc0,0xff,0xc0,0xff,0xc0,0xff,0xc0,0xff,0xff,0xff,0xff,0xff,0,0,0xff,0xff};
        if (out_length >= 36) reply(request, v2, sizeof(v2)); else reply(request, v1, sizeof(v1));
        return;
    }
    case 0x8000e00c: {
        uint8_t state[29]; ep_xusb_state(s, state, 0); reply(request, state, sizeof(state)); return;
    }
    case 0x8000a010:
        status = ep_rumble(s, input, in_length) ? STATUS_SUCCESS : STATUS_INVALID_PARAMETER; break;
    case 0x8000e008: { const uint8_t led[3] = {0,0,6}; reply(request, led, sizeof(led)); return; }
    case 0x8000e018: { const uint8_t battery[4] = {0,1,3,0}; reply(request, battery, sizeof(battery)); return; }
    case 0x8000e3ac:
        if (out_length < 29) { status = STATUS_BUFFER_TOO_SMALL; break; }
        status = WdfRequestForwardToIoQueue(request, ctx->waiting);
        if (NT_SUCCESS(status)) return;
        break;
    case 0x8000e3fc: {
        uint8_t info[64] = {3,1,1}; info[8] = 0x5e; info[9] = 4; info[10] = 0x8e; info[11] = 2;
        reply(request, info, sizeof(info)); return;
    }
    default: status = STATUS_INVALID_DEVICE_REQUEST;
    }
    WdfRequestComplete(request, status);
}
