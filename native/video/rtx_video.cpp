// NVIDIA's D3D11 video extension is also used by Chromium's swap_chain_presenter.
// Only the system graphics driver is used; no NGX models or SDK DLLs are bundled.
#include <windows.h>
#include <d3d11_1.h>
#include <dxgi1_2.h>
#include <gl/GL.h>
#include <wrl/client.h>
#include <algorithm>
#include <array>
#include <cstdio>
#include <cstring>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

using Microsoft::WRL::ComPtr;
namespace {
thread_local std::string error;
void check(HRESULT hr, const char* operation) {
    if (FAILED(hr)) {
        char code[20];
        sprintf_s(code, "0x%08lX", static_cast<unsigned long>(hr));
        throw std::runtime_error(std::string(operation) + ": " + code);
    }
}
template<class T> T proc(const char* name) {
    auto address = wglGetProcAddress(name);
    if (!address || address == reinterpret_cast<PROC>(1) || address == reinterpret_cast<PROC>(2)
        || address == reinterpret_cast<PROC>(3) || address == reinterpret_cast<PROC>(-1))
        throw std::runtime_error(std::string("Missing OpenGL interop: ") + name);
    return reinterpret_cast<T>(address);
}
constexpr GUID nvidia_video = {0xd43ce1b3, 0x1f4b, 0x48ac, {0xba,0xee,0xc3,0xc2,0x53,0x75,0xe6,0xf7}};
struct Timing {
    ComPtr<ID3D11Query> disjoint, begin, end;
    bool pending = false;
};
class Video {
public:
    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> context;
    ComPtr<ID3D11VideoDevice> video_device;
    ComPtr<ID3D11VideoContext1> video_context;
    ComPtr<ID3D11VideoProcessorEnumerator> enumerator;
    ComPtr<ID3D11VideoProcessor> processor;
    ComPtr<ID3D11Texture2D> input, output, readback;
    ComPtr<ID3D11VideoProcessorInputView> input_view;
    ComPtr<ID3D11VideoProcessorOutputView> output_view;
    using Open = HANDLE(WINAPI*)(void*);
    using Close = BOOL(WINAPI*)(HANDLE);
    using Register = HANDLE(WINAPI*)(HANDLE, void*, GLuint, GLenum, GLenum);
    using Unregister = BOOL(WINAPI*)(HANDLE, HANDLE);
    using Access = BOOL(WINAPI*)(HANDLE, GLint, HANDLE*);
    Open open = nullptr;
    Close close = nullptr;
    Register register_object = nullptr;
    Unregister unregister_object = nullptr;
    Access lock = nullptr, unlock = nullptr;
    HANDLE interop = nullptr, object = nullptr;
    bool locked = false;
    bool enabled;
    UINT width, height, out_width, out_height, sequence = 0;
    std::vector<unsigned char> pixels;
    std::array<Timing, 4> timings;
    double gpu_ms = -1;
    double effect_difference = 0;

    Video(UINT w, UINT h, UINT ow, UINT oh, GLuint texture, bool use_vsr)
        : enabled(use_vsr), width(w), height(h), out_width(ow), out_height(oh) {
        if (!w || !h || w%2 || h%2 || ow<w || oh<h || ow>7680 || oh>4320)
            throw std::runtime_error("Invalid RTX video dimensions");
        pixels.resize(size_t(w)*h*3/2);
        ComPtr<IDXGIFactory1> factory;
        check(CreateDXGIFactory1(IID_PPV_ARGS(&factory)), "Create DXGI factory");
        ComPtr<IDXGIAdapter1> adapter;
        for (UINT i=0; factory->EnumAdapters1(i, &adapter) != DXGI_ERROR_NOT_FOUND; ++i) {
            DXGI_ADAPTER_DESC1 desc{};
            check(adapter->GetDesc1(&desc), "Read GPU adapter");
            if (desc.VendorId == 0x10de && !(desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE)) break;
            adapter.Reset();
        }
        if (!adapter) throw std::runtime_error("RTX VSR requires an NVIDIA adapter");
        check(D3D11CreateDevice(adapter.Get(), D3D_DRIVER_TYPE_UNKNOWN, nullptr,
            D3D11_CREATE_DEVICE_VIDEO_SUPPORT | D3D11_CREATE_DEVICE_BGRA_SUPPORT,
            nullptr, 0, D3D11_SDK_VERSION, &device, nullptr, &context), "Create D3D11 device");
        check(device.As(&video_device), "Create video device");
        check(context.As(&video_context), "Create video context");
        D3D11_VIDEO_PROCESSOR_CONTENT_DESC desc{};
        desc.InputFrameFormat = D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE;
        desc.InputFrameRate = desc.OutputFrameRate = {90, 1};
        desc.InputWidth=w; desc.InputHeight=h; desc.OutputWidth=ow; desc.OutputHeight=oh;
        desc.Usage=D3D11_VIDEO_USAGE_PLAYBACK_NORMAL;
        check(video_device->CreateVideoProcessorEnumerator(&desc, &enumerator), "Enumerate video processor");
        check(video_device->CreateVideoProcessor(enumerator.Get(), 0, &processor), "Create video processor");
        video_context->VideoProcessorSetStreamAutoProcessingMode(processor.Get(), 0, FALSE);
        video_context->VideoProcessorSetStreamFrameFormat(processor.Get(), 0, D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE);
        video_context->VideoProcessorSetStreamOutputRate(processor.Get(), 0, D3D11_VIDEO_PROCESSOR_OUTPUT_RATE_NORMAL, TRUE, nullptr);
        RECT source{0,0,LONG(w),LONG(h)}, target{0,0,LONG(ow),LONG(oh)};
        video_context->VideoProcessorSetStreamSourceRect(processor.Get(), 0, TRUE, &source);
        video_context->VideoProcessorSetStreamDestRect(processor.Get(), 0, TRUE, &target);
        video_context->VideoProcessorSetOutputTargetRect(processor.Get(), TRUE, &target);
        video_context->VideoProcessorSetOutputColorSpace1(processor.Get(), DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709);
        set_enabled(enabled);
        D3D11_TEXTURE2D_DESC tex{};
        tex.Width=w; tex.Height=h; tex.MipLevels=1; tex.ArraySize=1;
        tex.Format=DXGI_FORMAT_NV12; tex.SampleDesc.Count=1;
        tex.Usage=D3D11_USAGE_DEFAULT; tex.BindFlags=D3D11_BIND_DECODER;
        check(device->CreateTexture2D(&tex, nullptr, &input), "Create NV12 input");
        D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC iv{};
        iv.ViewDimension=D3D11_VPIV_DIMENSION_TEXTURE2D;
        check(video_device->CreateVideoProcessorInputView(input.Get(), enumerator.Get(), &iv, &input_view), "Create video input view");
        tex.Width=ow; tex.Height=oh; tex.Format=DXGI_FORMAT_B8G8R8A8_UNORM;
        tex.BindFlags=D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
        tex.MiscFlags=D3D11_RESOURCE_MISC_SHARED;
        check(device->CreateTexture2D(&tex, nullptr, &output), "Create RGB output");
        D3D11_VIDEO_PROCESSOR_OUTPUT_VIEW_DESC ov{};
        ov.ViewDimension=D3D11_VPOV_DIMENSION_TEXTURE2D;
        check(video_device->CreateVideoProcessorOutputView(output.Get(), enumerator.Get(), &ov, &output_view), "Create video output view");
        for (auto& time : timings) {
            D3D11_QUERY_DESC query{D3D11_QUERY_TIMESTAMP_DISJOINT, 0};
            check(device->CreateQuery(&query, &time.disjoint), "Create GPU timer");
            query.Query=D3D11_QUERY_TIMESTAMP;
            check(device->CreateQuery(&query, &time.begin), "Create GPU timestamp");
            check(device->CreateQuery(&query, &time.end), "Create GPU timestamp");
        }
        if (enabled) verify_effect();
        if (texture) {
            open=proc<Open>("wglDXOpenDeviceNV"); close=proc<Close>("wglDXCloseDeviceNV");
            register_object=proc<Register>("wglDXRegisterObjectNV");
            unregister_object=proc<Unregister>("wglDXUnregisterObjectNV");
            lock=proc<Access>("wglDXLockObjectsNV"); unlock=proc<Access>("wglDXUnlockObjectsNV");
            interop=open(device.Get());
            if (!interop) throw std::runtime_error("Cannot share D3D11 device with this OpenGL GPU");
            object=register_object(interop, output.Get(), texture, GL_TEXTURE_2D, 0); // WGL_ACCESS_READ_ONLY_NV
            if (!object) {
                close(interop); interop=nullptr;
                throw std::runtime_error("Cannot share RTX output texture with OpenGL");
            }
        }
    }
    ~Video() {
        if (object) {glBindTexture(GL_TEXTURE_2D, 0); glFinish();}
        if (locked) unlock(interop, 1, &object);
        if (object) unregister_object(interop, object);
        if (interop) close(interop);
    }
    void set_enabled(bool use_vsr) {
        struct {UINT version, method, enable;} extension{1, 2, use_vsr ? 1u : 0u};
        check(video_context->VideoProcessorSetStreamExtension(processor.Get(), 0,
            &nvidia_video, sizeof(extension), &extension), "NVIDIA RTX VSR extension");
    }
    void render(const unsigned char* y, UINT yp, const unsigned char* u, UINT up,
                const unsigned char* v, UINT vp, bool nv12, int matrix, bool full) {
        if (!y || !u || yp<width || up<(nv12 ? width : width/2) || (!nv12 && (!v || vp<width/2)))
            throw std::runtime_error("Invalid video planes");
        if (locked) {
            if (!unlock(interop, 1, &object)) throw std::runtime_error("Cannot release shared video texture");
            locked=false;
        }
        for (UINT row=0; row<height; ++row) memcpy(pixels.data()+size_t(row)*width, y+size_t(row)*yp, width);
        auto chroma=pixels.data()+size_t(width)*height;
        for (UINT row=0; row<height/2; ++row) {
            if (nv12) memcpy(chroma+size_t(row)*width, u+size_t(row)*up, width);
            else for (UINT x=0; x<width/2; ++x) {
                chroma[size_t(row)*width+2*x]=u[size_t(row)*up+x];
                chroma[size_t(row)*width+2*x+1]=v[size_t(row)*vp+x];
            }
        }
        Timing* active=nullptr;
        gpu_ms=-1;
        for (auto& time : timings) {
            if (time.pending) {
                D3D11_QUERY_DATA_TIMESTAMP_DISJOINT d{}; UINT64 a=0,b=0;
                if (context->GetData(time.disjoint.Get(), &d, sizeof(d), D3D11_ASYNC_GETDATA_DONOTFLUSH)==S_OK
                    && context->GetData(time.begin.Get(), &a, sizeof(a), D3D11_ASYNC_GETDATA_DONOTFLUSH)==S_OK
                    && context->GetData(time.end.Get(), &b, sizeof(b), D3D11_ASYNC_GETDATA_DONOTFLUSH)==S_OK) {
                    if (!d.Disjoint && d.Frequency && b>=a) gpu_ms=double(b-a)*1000/d.Frequency;
                    time.pending=false;
                }
            }
            if (!time.pending && !active) active=&time;
        }
        if (active) {context->Begin(active->disjoint.Get()); context->End(active->begin.Get());}
        context->UpdateSubresource(input.Get(), 0, nullptr, pixels.data(), width, 0);
        auto color = matrix==1 ? (full ? DXGI_COLOR_SPACE_YCBCR_FULL_G22_LEFT_P709 : DXGI_COLOR_SPACE_YCBCR_STUDIO_G22_LEFT_P709)
                              : (full ? DXGI_COLOR_SPACE_YCBCR_FULL_G22_LEFT_P601 : DXGI_COLOR_SPACE_YCBCR_STUDIO_G22_LEFT_P601);
        video_context->VideoProcessorSetStreamColorSpace1(processor.Get(), 0, color);
        set_enabled(enabled);
        D3D11_VIDEO_PROCESSOR_STREAM stream{};
        stream.Enable=TRUE; stream.pInputSurface=input_view.Get(); stream.InputFrameOrField=sequence++;
        check(video_context->VideoProcessorBlt(processor.Get(), output_view.Get(), 0, 1, &stream), "RTX video processing");
        if (active) {context->End(active->end.Get()); context->End(active->disjoint.Get()); active->pending=true;}
        context->Flush();
        if (object) {
            if (!lock(interop, 1, &object)) throw std::runtime_error("Cannot acquire shared video texture");
            locked=true;
        }
        check(device->GetDeviceRemovedReason(), "GPU device removed");
    }
    void download(unsigned char* target, UINT pitch) {
        if (!target || pitch<out_width*4 || object) throw std::runtime_error("Readback is for headless validation only");
        if (!readback) {
            D3D11_TEXTURE2D_DESC desc{}; output->GetDesc(&desc);
            desc.Usage=D3D11_USAGE_STAGING; desc.BindFlags=0; desc.CPUAccessFlags=D3D11_CPU_ACCESS_READ; desc.MiscFlags=0;
            check(device->CreateTexture2D(&desc, nullptr, &readback), "Create validation readback");
        }
        context->CopyResource(readback.Get(), output.Get());
        D3D11_MAPPED_SUBRESOURCE mapped{};
        check(context->Map(readback.Get(), 0, D3D11_MAP_READ, 0, &mapped), "Read validation pixels");
        for (UINT row=0; row<out_height; ++row)
            memcpy(target+size_t(row)*pitch, static_cast<unsigned char*>(mapped.pData)+size_t(row)*mapped.RowPitch, out_width*4);
        context->Unmap(readback.Get(), 0);
    }
    void verify_effect() {
        // A successful extension HRESULT can still mean a disabled driver
        // feature. Probe only generated luma detail before sharing the output.
        for (UINT row=0; row<height; ++row) for (UINT x=0; x<width; ++x)
            pixels[size_t(row)*width+x]=static_cast<unsigned char>(32+((x*17+row*31+(x/7)*43)%192));
        std::fill(pixels.begin()+size_t(width)*height, pixels.end(), static_cast<unsigned char>(128));
        const auto pattern=pixels;
        auto probe=[this,&pattern]() {render(pattern.data(),width,pattern.data()+size_t(width)*height,width,nullptr,0,true,1,false);};
        std::vector<unsigned char> before(size_t(out_width)*out_height*4), after(before.size());
        enabled=false;
        probe();
        download(before.data(),out_width*4);
        enabled=true;
        for (int i=0;i<8;++i) probe();
        download(after.data(),out_width*4);
        double difference=0;
        for (size_t i=0;i<before.size();++i) if (i%4!=3) difference+=abs(int(before[i])-int(after[i]));
        effect_difference=difference/(size_t(out_width)*out_height*3);
        readback.Reset();
        if (effect_difference<.01)
            throw std::runtime_error("RTX VSR driver is inactive; enable RTX Video Super Resolution in NVIDIA settings and reconnect");
    }
};
}
#define API extern "C" __declspec(dllexport)
API const char* elink_video_error() {return error.c_str();}
API void* elink_video_create(UINT w, UINT h, UINT ow, UINT oh, UINT texture, int enabled) {
    try {return new Video(w,h,ow,oh,texture,enabled!=0);}
    catch (const std::exception& e) {error=e.what(); return nullptr;}
}
API void elink_video_destroy(Video* video) {delete video;}
API int elink_video_render(Video* video, const unsigned char* y, UINT yp, const unsigned char* u, UINT up,
                          const unsigned char* v, UINT vp, int nv12, int matrix, int full) {
    try {video->render(y,yp,u,up,v,vp,nv12!=0,matrix,full!=0); return 1;}
    catch (const std::exception& e) {error=e.what(); return 0;}
}
API double elink_video_gpu_ms(Video* video) {return video->gpu_ms;}
API double elink_video_effect(Video* video) {return video->effect_difference;}
API int elink_video_download(Video* video, unsigned char* target, UINT pitch) {
    try {video->download(target,pitch); return 1;}
    catch (const std::exception& e) {error=e.what(); return 0;}
}
