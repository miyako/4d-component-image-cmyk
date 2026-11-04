#include "image.hpp"
#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <limits>
#include <memory>
#include <map>
#include <setjmp.h>
#include <lcms2.h>
#include <jpeglib.h>
#include <png.h>
#include <tiffio.h>
#ifdef _WIN32
#define NOMINMAX
#include <windows.h>
#include <fcntl.h>
#include <io.h>
#else
#include <fcntl.h>
#include <unistd.h>
#endif

namespace image_cmyk {
namespace {
constexpr std::size_t maxInput = 256u * 1024u * 1024u;
constexpr std::size_t maxICC = 32u * 1024u * 1024u;
constexpr std::uint64_t maxPixels = 40000000;
[[noreturn]] void fail(const std::string& code, const std::string& message) { throw Error(code, message); }
std::size_t pixelSize(std::uint32_t w, std::uint32_t h, unsigned c) {
    const std::uint64_t pixels = std::uint64_t(w) * h;
    if (!w || !h || pixels > maxPixels || c < 3 || c > 4 || pixels * c > maxInput)
        fail("image_limit", "image exceeds pixel or decoded-byte limit");
    return static_cast<std::size_t>(pixels * c);
}
void checkOrientation(unsigned o) {
    if (o != 1) fail("unsupported_orientation", "only top-left orientation (1) is supported");
}
void checkDPI(double x, double y) {
    if (!std::isfinite(x) || !std::isfinite(y) || x < 0 || y < 0 ||
        x > 1000000 || y > 1000000)
        fail("invalid_image", "invalid image resolution");
}
using Profile = std::unique_ptr<void, decltype(&cmsCloseProfile)>;
Profile profile(const std::vector<unsigned char>& bytes) {
    if (bytes.size() < 128 || bytes.size() > maxICC)
        fail("invalid_profile", "ICC profile size is invalid");
    // Reject truncated profiles and trailing data rather than silently accepting a prefix.
    const std::uint32_t declared = (std::uint32_t(bytes[0]) << 24) |
        (std::uint32_t(bytes[1]) << 16) | (std::uint32_t(bytes[2]) << 8) | bytes[3];
    if (declared != bytes.size()) fail("invalid_profile", "ICC declared size does not match data");
    Profile p(cmsOpenProfileFromMem(bytes.data(), static_cast<cmsUInt32Number>(bytes.size())), cmsCloseProfile);
    if (!p) fail("invalid_profile", "Little CMS rejected ICC profile");
    const auto space = cmsGetColorSpace(p.get());
    if (space != cmsSigRgbData && space != cmsSigCmykData)
        fail("unsupported_profile", "ICC profile must describe RGB or CMYK");
    if (cmsGetDeviceClass(p.get()) == cmsSigLinkClass ||
        cmsGetDeviceClass(p.get()) == cmsSigNamedColorClass ||
        cmsGetDeviceClass(p.get()) == cmsSigAbstractClass)
        fail("unsupported_profile", "device-link, abstract and named-color ICC profiles are unsupported");
    return p;
}
Json profileInfo(cmsHPROFILE p, std::size_t size) {
    char description[1024] = {};
    cmsGetProfileInfoASCII(p, cmsInfoDescription, "en", "US", description, sizeof(description));
    std::string text(description);
    if (!validUtf8(text)) text = "ICC profile";
    return {{"colorSpace", cmsGetColorSpace(p) == cmsSigCmykData ? "CMYK" : "RGB"},
        {"description", text}, {"bytes", size}};
}
std::vector<unsigned char> saveProfile(cmsHPROFILE p) {
    cmsUInt32Number n = 0;
    if (!cmsSaveProfileToMem(p, nullptr, &n) || n > maxICC) fail("invalid_profile", "cannot serialize ICC profile");
    std::vector<unsigned char> result(n);
    if (!cmsSaveProfileToMem(p, result.data(), &n)) fail("invalid_profile", "cannot serialize ICC profile");
    return result;
}
// Only the bounded TIFF IFD0 fields needed for EXIF orientation/resolution are read.
void exif(const unsigned char* b, std::size_t n, Image& image) {
    if (n < 8) fail("invalid_image", "truncated EXIF header");
    const bool le = b[0] == 'I' && b[1] == 'I';
    if (!le && !(b[0] == 'M' && b[1] == 'M')) fail("invalid_image", "invalid EXIF byte order");
    auto u16 = [&](std::size_t o) -> std::uint32_t {
        if (o > n || n - o < 2) fail("invalid_image", "truncated EXIF field");
        return le ? b[o] | (std::uint32_t(b[o + 1]) << 8) : (std::uint32_t(b[o]) << 8) | b[o + 1];
    };
    auto u32 = [&](std::size_t o) -> std::uint32_t {
        if (o > n || n - o < 4) fail("invalid_image", "truncated EXIF field");
        return le ? b[o] | (std::uint32_t(b[o + 1]) << 8) | (std::uint32_t(b[o + 2]) << 16) |
            (std::uint32_t(b[o + 3]) << 24) :
            (std::uint32_t(b[o]) << 24) | (std::uint32_t(b[o + 1]) << 16) |
            (std::uint32_t(b[o + 2]) << 8) | b[o + 3];
    };
    if (u16(2) != 42) fail("invalid_image", "invalid EXIF TIFF signature");
    const std::size_t ifd = u32(4);
    const auto count = u16(ifd);
    if (ifd > n || std::uint64_t(ifd) + 2 + std::uint64_t(count) * 12 + 4 > n)
        fail("invalid_image", "invalid EXIF directory");
    double x = 0, y = 0;
    unsigned unit = 2;
    for (unsigned i = 0; i < count; ++i) {
        const auto o = ifd + 2 + i * 12;
        const auto tag = u16(o), type = u16(o + 2), num = u32(o + 4);
        if (tag == 274 || tag == 296) {
            if (type != 3 || num != 1) fail("invalid_image", "invalid EXIF scalar");
            if (tag == 274) image.orientation = u16(o + 8); else unit = u16(o + 8);
        } else if (tag == 282 || tag == 283) {
            if (type != 5 || num != 1) fail("invalid_image", "invalid EXIF resolution");
            const auto v = u32(o + 8), den = u32(std::size_t(v) + 4);
            if (!den) fail("invalid_image", "zero EXIF resolution denominator");
            const double value = double(u32(v)) / den;
            if (tag == 282) x = value; else y = value;
        }
    }
    checkOrientation(image.orientation);
    if (unit == 2 || unit == 3) {
        if (x) image.dpiX = x * (unit == 3 ? 2.54 : 1);
        if (y) image.dpiY = y * (unit == 3 ? 2.54 : 1);
    }
}

struct JpegError { jpeg_error_mgr base; jmp_buf jump; };
void jpegError(j_common_ptr c) { longjmp(reinterpret_cast<JpegError*>(c->err)->jump, 1); }
void jpegSilent(j_common_ptr) {}
struct JpegDecode {
    jpeg_decompress_struct c{};
    JpegError error{};
    ~JpegDecode() { if (c.mem) jpeg_destroy_decompress(&c); }
};
Image decodeJpeg(const std::vector<unsigned char>& data) {
    auto s = std::make_unique<JpegDecode>();
    auto output = std::make_unique<Image>();
    auto segments = std::make_unique<std::array<std::vector<unsigned char>, 256>>();
    auto& out = *output;
    auto& parts = *segments;
    s->c.err = jpeg_std_error(&s->error.base);
    s->error.base.error_exit = jpegError;
    s->error.base.output_message = jpegSilent;
    if (setjmp(s->error.jump)) fail("invalid_image", "JPEG decoding failed");
    jpeg_create_decompress(&s->c);
    jpeg_mem_src(&s->c, data.data(), static_cast<unsigned long>(data.size()));
    jpeg_save_markers(&s->c, JPEG_APP0 + 1, 0xffff);
    jpeg_save_markers(&s->c, JPEG_APP0 + 2, 0xffff);
    jpeg_read_header(&s->c, TRUE);
    const bool cmyk = s->c.jpeg_color_space == JCS_CMYK || s->c.jpeg_color_space == JCS_YCCK;
    if (!cmyk && s->c.jpeg_color_space != JCS_RGB && s->c.jpeg_color_space != JCS_YCbCr)
        fail("unsupported_image", "JPEG must be RGB, CMYK or YCCK");
    if (s->c.saw_Adobe_marker && ((cmyk && s->c.Adobe_transform != 0 && s->c.Adobe_transform != 2) ||
        (!cmyk && s->c.Adobe_transform != 0 && s->c.Adobe_transform != 1)))
        fail("unsupported_image", "unsupported JPEG Adobe transform");
    out.width = s->c.image_width; out.height = s->c.image_height;
    out.channels = cmyk ? 4 : 3; out.format = "jpeg";
    const auto bytes = pixelSize(out.width, out.height, out.channels);
    if (s->c.density_unit == 1 || s->c.density_unit == 2) {
        const double factor = s->c.density_unit == 2 ? 2.54 : 1;
        out.dpiX = s->c.X_density * factor; out.dpiY = s->c.Y_density * factor;
    }
    unsigned total = 0, found = 0;
    std::size_t profileBytes = 0;
    for (auto m = s->c.marker_list; m; m = m->next) {
        if (m->marker == JPEG_APP0 + 1 && m->data_length >= 6 && !std::memcmp(m->data, "Exif\0\0", 6))
            exif(m->data + 6, m->data_length - 6, out);
        if (m->marker != JPEG_APP0 + 2 || m->data_length < 12 ||
            std::memcmp(m->data, "ICC_PROFILE\0", 12)) continue;
        if (m->data_length < 14) fail("invalid_profile", "truncated JPEG ICC marker");
        const unsigned seq = m->data[12], count = m->data[13];
        if (!seq || !count || seq > count || (total && total != count) || !parts[seq].empty())
            fail("invalid_profile", "invalid JPEG ICC sequence");
        total = count; ++found;
        if (m->data_length == 14) fail("invalid_profile", "empty JPEG ICC segment");
        profileBytes += m->data_length - 14;
        if (profileBytes > maxICC) fail("invalid_profile", "JPEG ICC profile exceeds limit");
        parts[seq].assign(m->data + 14, m->data + m->data_length);
    }
    if (found != total) fail("invalid_profile", "incomplete JPEG ICC profile");
    for (unsigned i = 1; i <= total; ++i) out.icc.insert(out.icc.end(), parts[i].begin(), parts[i].end());
    s->c.out_color_space = cmyk ? JCS_CMYK : JCS_RGB;
    jpeg_start_decompress(&s->c);
    if (s->c.output_width != out.width || s->c.output_height != out.height ||
        s->c.output_components != out.channels) fail("unsupported_image", "unexpected JPEG output layout");
    out.pixels.resize(bytes);
    while (s->c.output_scanline < s->c.output_height) {
        JSAMPROW row = out.pixels.data() + std::size_t(s->c.output_scanline) * out.width * out.channels;
        if (jpeg_read_scanlines(&s->c, &row, 1) != 1) fail("invalid_image", "incomplete JPEG pixels");
    }
    const bool invert = cmyk && s->c.saw_Adobe_marker;
    jpeg_finish_decompress(&s->c);
    if (s->error.base.num_warnings) fail("invalid_image", "JPEG contains damaged or truncated data");
    if (invert) for (auto& v : out.pixels) v = 255 - v;
    return std::move(out);
}

struct PngState {
    png_structp p = nullptr; png_infop info = nullptr;
    const std::vector<unsigned char>* data = nullptr; std::size_t offset = 0;
    ~PngState() { if (p) png_destroy_read_struct(&p, &info, nullptr); }
};
void pngError(png_structp p, png_const_charp) { png_longjmp(p, 1); }
void pngWarning(png_structp, png_const_charp) {}
void pngRead(png_structp p, png_bytep dst, png_size_t n) {
    auto s = static_cast<PngState*>(png_get_io_ptr(p));
    if (s->offset > s->data->size() || n > s->data->size() - s->offset) png_error(p, "truncated");
    std::memcpy(dst, s->data->data() + s->offset, n); s->offset += n;
}
Image decodePng(const std::vector<unsigned char>& data) {
    auto s = std::make_unique<PngState>();
    auto output = std::make_unique<Image>();
    auto& out = *output;
    s->data = &data;
    s->p = png_create_read_struct(PNG_LIBPNG_VER_STRING, nullptr, pngError, pngWarning);
    if (!s->p) throw std::bad_alloc();
    s->info = png_create_info_struct(s->p);
    if (!s->info) throw std::bad_alloc();
    if (setjmp(png_jmpbuf(s->p))) fail("invalid_image", "PNG decoding failed");
    png_set_read_fn(s->p, s.get(), pngRead);
    png_set_user_limits(s->p, 40000000, 40000000);
    png_set_chunk_malloc_max(s->p, maxICC);
    png_set_crc_action(s->p, PNG_CRC_ERROR_QUIT, PNG_CRC_ERROR_QUIT);
    png_read_info(s->p, s->info);
    out.width = png_get_image_width(s->p, s->info); out.height = png_get_image_height(s->p, s->info);
    out.channels = 3; out.format = "png";
    const auto color = png_get_color_type(s->p, s->info);
    if (color != PNG_COLOR_TYPE_RGB || png_get_bit_depth(s->p, s->info) != 8 ||
        png_get_valid(s->p, s->info, PNG_INFO_tRNS))
        fail("unsupported_image", "PNG requires 8-bit RGB without alpha or transparency");
    if (png_get_interlace_type(s->p, s->info) != PNG_INTERLACE_NONE)
        fail("unsupported_image", "interlaced PNG is unsupported");
    const auto bytes = pixelSize(out.width, out.height, 3);
    png_charp name = nullptr; int compression = 0; png_bytep icc = nullptr; png_uint_32 iccSize = 0;
    if (png_get_iCCP(s->p, s->info, &name, &compression, &icc, &iccSize)) {
        if (!iccSize || iccSize > maxICC) fail("invalid_profile", "PNG ICC profile exceeds limit");
        out.icc.assign(icc, icc + iccSize);
    }
    // libpng may discard invalid iCCP chunks; never downgrade a tagged source to untagged.
    bool tagged = false;
    for (std::size_t o = 8; o + 12 <= data.size();) {
        const auto n = (std::uint32_t(data[o]) << 24) | (std::uint32_t(data[o + 1]) << 16) |
            (std::uint32_t(data[o + 2]) << 8) | data[o + 3];
        if (n > data.size() - o - 12) fail("invalid_image", "truncated PNG chunk");
        if (!std::memcmp(data.data() + o + 4, "iCCP", 4)) {
            if (tagged) fail("invalid_profile", "duplicate PNG ICC profile");
            tagged = true;
        }
        if (!std::memcmp(data.data() + o + 4, "eXIf", 4)) exif(data.data() + o + 8, n, out);
        if (!std::memcmp(data.data() + o + 4, "acTL", 4)) fail("unsupported_image", "animated PNG is unsupported");
        o += n + 12;
    }
    if (tagged && out.icc.empty()) fail("invalid_profile", "PNG ICC profile was rejected");
    png_uint_32 x = 0, y = 0; int unit = 0;
    if (png_get_pHYs(s->p, s->info, &x, &y, &unit) && unit == PNG_RESOLUTION_METER) {
        out.dpiX = x * .0254; out.dpiY = y * .0254;
    }
    out.pixels.resize(bytes);
    for (unsigned yrow = 0; yrow < out.height; ++yrow)
        png_read_row(s->p, out.pixels.data() + std::size_t(yrow) * out.width * 3, nullptr);
    png_read_end(s->p, s->info);
    return std::move(out);
}

struct TiffMemory { std::vector<unsigned char> bytes; std::size_t offset = 0; bool failed = false; };
tmsize_t tiffRead(thandle_t h, void* p, tmsize_t n) {
    auto& m = *static_cast<TiffMemory*>(h);
    if (n < 0 || m.offset > m.bytes.size()) return -1;
    const auto amount = std::min<std::size_t>(n, m.bytes.size() - m.offset);
    std::memcpy(p, m.bytes.data() + m.offset, amount); m.offset += amount;
    return static_cast<tmsize_t>(amount);
}
tmsize_t tiffWrite(thandle_t h, void* p, tmsize_t n) {
    auto& m = *static_cast<TiffMemory*>(h);
    if (n < 0 || m.offset > maxInput || std::uint64_t(n) > maxInput - m.offset) { m.failed = true; return -1; }
    try {
        if (m.offset + n > m.bytes.size()) m.bytes.resize(m.offset + n);
        std::memcpy(m.bytes.data() + m.offset, p, n); m.offset += n;
        return n;
    } catch (...) { m.failed = true; return -1; }
}
toff_t tiffSeek(thandle_t h, toff_t o, int whence) {
    auto& m = *static_cast<TiffMemory*>(h);
    std::uint64_t value = o, base = 0;
    if (whence == SEEK_CUR) base = m.offset;
    else if (whence == SEEK_END) base = m.bytes.size();
    else if (whence != SEEK_SET) return toff_t(-1);
    if (whence != SEEK_SET) {
        if (o > std::numeric_limits<std::int64_t>::max()) {
            const auto distance = (~o) + 1;
            if (distance > base) return toff_t(-1);
            value = base - distance;
        } else {
            if (o > maxInput || base > maxInput - o) return toff_t(-1);
            value = base + o;
        }
    }
    if (value > maxInput) { m.failed = true; return toff_t(-1); }
    m.offset = static_cast<std::size_t>(value); return m.offset;
}
int tiffClose(thandle_t) { return 0; }
toff_t tiffSize(thandle_t h) { return static_cast<TiffMemory*>(h)->bytes.size(); }
int tiffMap(thandle_t, void**, toff_t*) { return 0; }
void tiffUnmap(thandle_t, void*, toff_t) {}
using Tiff = std::unique_ptr<TIFF, decltype(&TIFFClose)>;
int tiffSilent(TIFF*, void*, const char*, const char*, va_list) { return 1; }
Tiff openTiff(TiffMemory& mem, const char* mode) {
    std::unique_ptr<TIFFOpenOptions, decltype(&TIFFOpenOptionsFree)> options(TIFFOpenOptionsAlloc(), TIFFOpenOptionsFree);
    if (!options) throw std::bad_alloc();
    TIFFOpenOptionsSetMaxSingleMemAlloc(options.get(), maxInput);
    TIFFOpenOptionsSetMaxCumulatedMemAlloc(options.get(), maxInput);
    TIFFOpenOptionsSetErrorHandlerExtR(options.get(), tiffSilent, nullptr);
    TIFFOpenOptionsSetWarningHandlerExtR(options.get(), tiffSilent, nullptr);
    Tiff t(TIFFClientOpenExt("memory", mode, &mem, tiffRead, tiffWrite, tiffSeek,
        tiffClose, tiffSize, tiffMap, tiffUnmap, options.get()), TIFFClose);
    if (!t) fail("invalid_image", "cannot open TIFF");
    return t;
}
Image decodeTiff(const std::vector<unsigned char>& bytes) {
    TiffMemory memory{bytes};
    auto t = openTiff(memory, "r");
    if (!TIFFLastDirectory(t.get())) fail("unsupported_image", "TIFF must contain exactly one page");
    Image out; out.format = "tiff";
    std::uint16_t bits = 0, channels = 0, planar = 0, photo = 0, orientation = 1, sample = SAMPLEFORMAT_UINT;
    TIFFGetField(t.get(), TIFFTAG_IMAGEWIDTH, &out.width);
    TIFFGetField(t.get(), TIFFTAG_IMAGELENGTH, &out.height);
    TIFFGetFieldDefaulted(t.get(), TIFFTAG_BITSPERSAMPLE, &bits);
    TIFFGetFieldDefaulted(t.get(), TIFFTAG_SAMPLESPERPIXEL, &channels);
    TIFFGetFieldDefaulted(t.get(), TIFFTAG_PLANARCONFIG, &planar);
    TIFFGetFieldDefaulted(t.get(), TIFFTAG_ORIENTATION, &orientation);
    TIFFGetFieldDefaulted(t.get(), TIFFTAG_SAMPLEFORMAT, &sample);
    TIFFGetField(t.get(), TIFFTAG_PHOTOMETRIC, &photo);
    std::uint16_t extras = 0; std::uint16_t* extraInfo = nullptr;
    TIFFGetFieldDefaulted(t.get(), TIFFTAG_EXTRASAMPLES, &extras, &extraInfo);
    std::uint16_t inkset = INKSET_CMYK;
    TIFFGetFieldDefaulted(t.get(), TIFFTAG_INKSET, &inkset);
    std::uint16_t inks = 4, subCount = 0;
    std::uint64_t* subOffsets = nullptr;
    TIFFGetField(t.get(), TIFFTAG_NUMBEROFINKS, &inks);
    TIFFGetField(t.get(), TIFFTAG_SUBIFD, &subCount, &subOffsets);
    if (bits != 8 || planar != PLANARCONFIG_CONTIG || sample != SAMPLEFORMAT_UINT || extras ||
        subCount || TIFFIsTiled(t.get()) || !((photo == PHOTOMETRIC_RGB && channels == 3) ||
        (photo == PHOTOMETRIC_SEPARATED && channels == 4 && inkset == INKSET_CMYK && inks == 4)))
        fail("unsupported_image", "TIFF requires single-page stripped contiguous 8-bit RGB or CMYK without alpha");
    out.orientation = orientation; checkOrientation(orientation);
    out.channels = channels;
    out.pixels.resize(pixelSize(out.width, out.height, channels));
    if (TIFFScanlineSize64(t.get()) != std::uint64_t(out.width) * channels)
        fail("unsupported_image", "unexpected TIFF scanline layout");
    std::uint32_t iccSize = 0; void* icc = nullptr;
    if (TIFFGetField(t.get(), TIFFTAG_ICCPROFILE, &iccSize, &icc)) {
        if (!icc || !iccSize || iccSize > maxICC) fail("invalid_profile", "TIFF ICC profile size is invalid");
        const auto p = static_cast<unsigned char*>(icc); out.icc.assign(p, p + iccSize);
    }
    float x = 0, y = 0; std::uint16_t unit = RESUNIT_INCH;
    TIFFGetField(t.get(), TIFFTAG_XRESOLUTION, &x); TIFFGetField(t.get(), TIFFTAG_YRESOLUTION, &y);
    TIFFGetFieldDefaulted(t.get(), TIFFTAG_RESOLUTIONUNIT, &unit);
    if (unit == RESUNIT_INCH || unit == RESUNIT_CENTIMETER) {
        out.dpiX = x * (unit == RESUNIT_CENTIMETER ? 2.54 : 1);
        out.dpiY = y * (unit == RESUNIT_CENTIMETER ? 2.54 : 1);
    }
    for (unsigned yrow = 0; yrow < out.height; ++yrow)
        if (TIFFReadScanline(t.get(), out.pixels.data() + std::size_t(yrow) * out.width * channels, yrow, 0) < 0)
            fail("invalid_image", "TIFF scanline decoding failed");
    return out;
}
std::vector<unsigned char> encodeTiff(const Image& image) {
    TiffMemory memory;
    auto t = openTiff(memory, "w");
    auto field = [&](ttag_t tag, auto value) { if (!TIFFSetField(t.get(), tag, value)) fail("write_failed", "cannot set TIFF field"); };
    field(TIFFTAG_IMAGEWIDTH, image.width); field(TIFFTAG_IMAGELENGTH, image.height);
    field(TIFFTAG_BITSPERSAMPLE, 8); field(TIFFTAG_SAMPLESPERPIXEL, image.channels);
    field(TIFFTAG_PLANARCONFIG, PLANARCONFIG_CONTIG); field(TIFFTAG_ORIENTATION, image.orientation);
    field(TIFFTAG_PHOTOMETRIC, image.channels == 4 ? PHOTOMETRIC_SEPARATED : PHOTOMETRIC_RGB);
    field(TIFFTAG_COMPRESSION, COMPRESSION_NONE); field(TIFFTAG_ROWSPERSTRIP, 32);
    if (image.channels == 4) field(TIFFTAG_INKSET, INKSET_CMYK);
    if (image.dpiX > 0) field(TIFFTAG_XRESOLUTION, image.dpiX);
    if (image.dpiY > 0) field(TIFFTAG_YRESOLUTION, image.dpiY);
    field(TIFFTAG_RESOLUTIONUNIT, RESUNIT_INCH);
    if (!image.icc.empty() && !TIFFSetField(t.get(), TIFFTAG_ICCPROFILE, static_cast<std::uint32_t>(image.icc.size()), image.icc.data()))
        fail("write_failed", "cannot embed TIFF ICC profile");
    for (unsigned y = 0; y < image.height; ++y)
        if (TIFFWriteScanline(t.get(), const_cast<unsigned char*>(image.pixels.data()) + std::size_t(y) * image.width * image.channels, y, 0) < 0)
            fail("write_failed", "cannot encode TIFF pixels");
    if (!TIFFWriteDirectory(t.get())) fail("write_failed", "cannot finish TIFF");
    t.reset();
    if (memory.failed) fail("write_failed", "TIFF output exceeds limit");
    return std::move(memory.bytes);
}
struct JpegEncode {
    jpeg_compress_struct c{}; JpegError error{};
    unsigned char* data = nullptr; unsigned long size = 0;
    ~JpegEncode() { if (c.mem) jpeg_destroy_compress(&c); std::free(data); }
};
std::vector<unsigned char> encodeJpeg(const Image& image, int quality) {
    // Prepared before setjmp: no C++ object lifetime is skipped by libjpeg errors.
    std::vector<unsigned char> rowStorage(image.channels == 4 ? std::size_t(image.width) * 4 : 0);
    auto s = std::make_unique<JpegEncode>();
    s->c.err = jpeg_std_error(&s->error.base);
    s->error.base.error_exit = jpegError; s->error.base.output_message = jpegSilent;
    if (setjmp(s->error.jump)) fail("write_failed", "JPEG encoding failed");
    jpeg_create_compress(&s->c);
    jpeg_mem_dest(&s->c, &s->data, &s->size);
    s->c.image_width = image.width; s->c.image_height = image.height;
    s->c.input_components = image.channels; s->c.in_color_space = image.channels == 4 ? JCS_CMYK : JCS_RGB;
    jpeg_set_defaults(&s->c); jpeg_set_quality(&s->c, quality, TRUE);
    // CMYK JPEG uses Adobe transform 0 and inverted sample convention.
    s->c.write_Adobe_marker = image.channels == 4;
    if (image.dpiX > 0 && image.dpiY > 0 && image.dpiX <= 65535 && image.dpiY <= 65535) {
        s->c.density_unit = 1; s->c.X_density = static_cast<UINT16>(std::lround(image.dpiX));
        s->c.Y_density = static_cast<UINT16>(std::lround(image.dpiY));
        s->c.write_JFIF_header = image.channels == 3;
    }
    jpeg_start_compress(&s->c, TRUE);
    if (image.dpiX > 0 || image.dpiY > 0) {
        std::array<unsigned char, 84> marker{};
        std::memcpy(marker.data(), "Exif\0\0II", 8);
        auto u16 = [&](std::size_t o, unsigned v) { marker[o] = v & 255; marker[o + 1] = (v >> 8) & 255; };
        auto u32 = [&](std::size_t o, std::uint32_t v) {
            for (unsigned i = 0; i < 4; ++i) marker[o + i] = (v >> (8 * i)) & 255;
        };
        u16(8, 42); u32(10, 8); u16(14, 4);
        auto entry = [&](std::size_t o, unsigned tag, unsigned type, unsigned value) {
            u16(o, tag); u16(o + 2, type); u32(o + 4, 1); u32(o + 8, value);
        };
        entry(16, 274, 3, 1); entry(28, 282, 5, 62); entry(40, 283, 5, 70);
        entry(52, 296, 3, 2);
        const auto xDen = image.dpiX <= 429496 ? 10000u : 1000u;
        const auto yDen = image.dpiY <= 429496 ? 10000u : 1000u;
        u32(68, static_cast<std::uint32_t>(std::lround(image.dpiX * xDen))); u32(72, xDen);
        u32(76, static_cast<std::uint32_t>(std::lround(image.dpiY * yDen))); u32(80, yDen);
        jpeg_write_marker(&s->c, JPEG_APP0 + 1, marker.data(), marker.size());
    }
    constexpr std::size_t segment = 65519;
    const auto count = (image.icc.size() + segment - 1) / segment;
    if (count > 255) fail("unsupported_profile", "ICC profile exceeds JPEG marker capacity");
    for (std::size_t i = 0; i < count; ++i) {
        const auto n = std::min(segment, image.icc.size() - i * segment);
        std::array<unsigned char, 65533> marker{};
        std::memcpy(marker.data(), "ICC_PROFILE\0", 12);
        marker[12] = static_cast<unsigned char>(i + 1); marker[13] = static_cast<unsigned char>(count);
        std::memcpy(marker.data() + 14, image.icc.data() + i * segment, n);
        jpeg_write_marker(&s->c, JPEG_APP0 + 2, marker.data(), static_cast<unsigned>(n + 14));
    }
    while (s->c.next_scanline < s->c.image_height) {
        const auto input = image.pixels.data() + std::size_t(s->c.next_scanline) * image.width * image.channels;
        JSAMPROW row = const_cast<JSAMPROW>(input);
        if (image.channels == 4) {
            std::transform(input, input + rowStorage.size(), rowStorage.begin(), [](unsigned char v) { return 255 - v; });
            row = rowStorage.data();
        }
        jpeg_write_scanlines(&s->c, &row, 1);
    }
    jpeg_finish_compress(&s->c);
    if (s->size > maxInput) fail("image_limit", "JPEG output exceeds limit");
    return {s->data, s->data + s->size};
}
}

Error::Error(std::string c, std::string m) : std::runtime_error(std::move(m)), code(std::move(c)) {}
bool validUtf8(const std::string& s) {
    for (std::size_t i = 0; i < s.size();) {
        unsigned c = static_cast<unsigned char>(s[i++]), n = 0, min = 0;
        if (c < 128) continue;
        if (c >= 194 && c <= 223) { c &= 31; n = 1; min = 128; }
        else if (c >= 224 && c <= 239) { c &= 15; n = 2; min = 2048; }
        else if (c >= 240 && c <= 244) { c &= 7; n = 3; min = 65536; }
        else return false;
        if (n > s.size() - i) return false;
        while (n--) { const unsigned b = static_cast<unsigned char>(s[i++]); if ((b & 192) != 128) return false; c = (c << 6) | (b & 63); }
        if (c < min || c > 1114111 || (c >= 55296 && c <= 57343)) return false;
    }
    return true;
}
std::vector<unsigned char> readFile(const std::filesystem::path& path, std::size_t limit) {
    std::error_code ec;
    if (!std::filesystem::is_regular_file(path, ec)) fail("read_failed", "input is not a readable regular file");
    const auto size = std::filesystem::file_size(path, ec);
    if (ec) fail("read_failed", "cannot determine input size");
    if (size > limit) fail("input_limit", "input file exceeds byte limit");
    std::ifstream in(path, std::ios::binary);
    if (!in) fail("read_failed", "cannot open input file");
    std::vector<unsigned char> bytes(static_cast<std::size_t>(size));
    if (size && !in.read(reinterpret_cast<char*>(bytes.data()), static_cast<std::streamsize>(size)))
        fail("read_failed", "cannot read complete input");
    if (in.peek() != std::char_traits<char>::eof()) fail("read_failed", "input changed while reading");
    return bytes;
}
Image decode(const std::filesystem::path& path) {
    const auto data = readFile(path, maxInput);
    Image out;
    if (data.size() >= 2 && data[0] == 255 && data[1] == 216) out = decodeJpeg(data);
    else if (data.size() >= 8 && !png_sig_cmp(data.data(), 0, 8)) out = decodePng(data);
    else if (data.size() >= 4 && ((data[0] == 'I' && data[1] == 'I' && data[2] == 42 && data[3] == 0) ||
        (data[0] == 'M' && data[1] == 'M' && data[2] == 0 && data[3] == 42))) out = decodeTiff(data);
    else fail("unsupported_image", "expected JPEG, PNG or classic TIFF");
    checkOrientation(out.orientation); checkDPI(out.dpiX, out.dpiY);
    if (!out.icc.empty()) {
        auto p = profile(out.icc);
        if ((cmsGetColorSpace(p.get()) == cmsSigCmykData ? 4u : 3u) != out.channels)
            fail("profile_mismatch", "embedded ICC color space does not match image channels");
    }
    return out;
}
Json inspect(const std::filesystem::path& path) {
    const auto im = decode(path);
    Json embedded = {{"present", !im.icc.empty()}};
    if (!im.icc.empty()) { auto p = profile(im.icc); embedded.update(profileInfo(p.get(), im.icc.size())); }
    return {{"protocol", 1}, {"width", im.width}, {"height", im.height}, {"channels", im.channels},
        {"colorSpace", im.channels == 4 ? "CMYK" : "RGB"}, {"bitDepth", 8}, {"alpha", false},
        {"format", im.format}, {"orientation", im.orientation}, {"dpiX", im.dpiX}, {"dpiY", im.dpiY},
        {"embeddedProfile", embedded}};
}
std::vector<unsigned char> encode(const Image& image, const std::string& format, int quality) {
    if (image.pixels.size() != pixelSize(image.width, image.height, image.channels))
        fail("invalid_image", "pixel buffer size mismatch");
    if (image.icc.size() > maxICC) fail("invalid_profile", "ICC profile exceeds limit");
    if (format == "tiff") return encodeTiff(image);
    if (format == "jpeg" && quality >= 1 && quality <= 100) return encodeJpeg(image, quality);
    fail("invalid_arguments", "unsupported output format or quality");
}
void writeNew(const std::filesystem::path& destination, const std::vector<unsigned char>& bytes) {
    static std::atomic<unsigned long> serial{0};
    std::error_code ec;
    if (std::filesystem::symlink_status(destination, ec).type() != std::filesystem::file_type::not_found &&
        !ec) fail("destination_exists", "destination already exists (no overwrite)");
    auto parent = destination.parent_path();
    if (parent.empty()) parent = ".";
    if (!std::filesystem::is_directory(parent)) fail("write_failed", "destination directory does not exist");
    std::filesystem::path temporary;
    FILE* file = nullptr;
    for (unsigned attempt = 0; attempt < 100 && !file; ++attempt) {
#ifdef _WIN32
        const auto pid = GetCurrentProcessId();
#else
        const auto pid = getpid();
#endif
        temporary = parent / (".image-cmyk-" + std::to_string(pid) + "-" + std::to_string(serial++) + ".partial");
#ifdef _WIN32
        const HANDLE h = CreateFileW(temporary.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (h == INVALID_HANDLE_VALUE) { if (GetLastError() == ERROR_FILE_EXISTS) continue; fail("write_failed", "cannot create staging file"); }
        const int fd = _open_osfhandle(reinterpret_cast<intptr_t>(h), _O_BINARY);
        if (fd < 0) { CloseHandle(h); std::filesystem::remove(temporary, ec); fail("write_failed", "cannot open staging stream"); }
        file = _fdopen(fd, "wb");
        if (!file) _close(fd);
#else
        const int fd = ::open(temporary.c_str(), O_WRONLY | O_CREAT | O_EXCL, 0600);
        if (fd < 0) { if (errno == EEXIST) continue; fail("write_failed", "cannot create staging file"); }
        file = fdopen(fd, "wb");
        if (!file) ::close(fd);
#endif
        if (!file) { std::filesystem::remove(temporary, ec); fail("write_failed", "cannot open staging stream"); }
    }
    if (!file) fail("write_failed", "cannot allocate unique staging file");
    try {
        if (std::fwrite(bytes.data(), 1, bytes.size(), file) != bytes.size() || std::fflush(file))
            fail("write_failed", "cannot write complete staging file");
#ifdef _WIN32
        if (_commit(_fileno(file))) fail("write_failed", "cannot flush staging file");
#else
        if (fsync(fileno(file))) fail("write_failed", "cannot flush staging file");
#endif
        const int closed = std::fclose(file); file = nullptr;
        if (closed) fail("write_failed", "cannot close staging file");
#ifdef _WIN32
        if (!MoveFileExW(temporary.c_str(), destination.c_str(), MOVEFILE_WRITE_THROUGH)) {
            const auto e = GetLastError();
            fail(e == ERROR_ALREADY_EXISTS || e == ERROR_FILE_EXISTS ? "destination_exists" : "write_failed",
                "cannot publish destination without overwrite");
        }
#else
        if (::link(temporary.c_str(), destination.c_str())) {
            fail(errno == EEXIST ? "destination_exists" : "write_failed", "cannot publish destination without overwrite");
        }
        if (!std::filesystem::remove(temporary, ec) || ec) fail("write_failed", "output published but staging cleanup failed");
#endif
    } catch (...) {
        if (file) std::fclose(file);
        std::filesystem::remove(temporary, ec);
        throw;
    }
}
Json convert(const Options& o) {
    const std::map<std::string, int> intents{{"relativeColorimetric", INTENT_RELATIVE_COLORIMETRIC},
        {"perceptual", INTENT_PERCEPTUAL}, {"saturation", INTENT_SATURATION}, {"absoluteColorimetric", INTENT_ABSOLUTE_COLORIMETRIC}};
    if (!intents.count(o.intent) || (o.format != "tiff" && o.format != "jpeg") || o.quality < 1 || o.quality > 100)
        fail("invalid_arguments", "invalid rendering intent, format or quality");
    // Existence checks include symlinks/hardlinks; atomic publication repeats the no-clobber gate.
    std::error_code ec;
    if (std::filesystem::exists(o.destination, ec) || std::filesystem::is_symlink(o.destination, ec))
        fail("destination_exists", "destination already exists (source and aliases are never overwritten)");
    auto im = decode(o.source);
    if (!im.icc.empty() && !o.sourceProfile.empty())
        fail("profile_override", "a tagged source cannot be overridden by a supplied profile");
    std::string origin;
    Json warnings = Json::array();
    Profile source(nullptr, cmsCloseProfile);
    std::size_t sourceBytes = 0;
    if (!im.icc.empty()) {
        source = profile(im.icc); sourceBytes = im.icc.size(); origin = "embedded";
    } else if (!o.sourceProfile.empty()) {
        auto data = readFile(o.sourceProfile, maxICC); sourceBytes = data.size(); source = profile(data); origin = "supplied";
    } else if (im.channels == 3 && o.assumeSRGB) {
        source.reset(cmsCreate_sRGBProfile());
        if (!source) fail("invalid_profile", "cannot create sRGB profile");
        origin = "assumed-srgb"; warnings.push_back("untagged RGB source assumed sRGB");
    } else fail("missing_source_profile", "untagged images require a source profile; only RGB may explicitly assume sRGB");
    if ((cmsGetColorSpace(source.get()) == cmsSigCmykData ? 4u : 3u) != im.channels)
        fail("profile_mismatch", "source profile color space does not match image channels");
    const auto destBytes = readFile(o.destinationProfile, maxICC);
    auto dest = profile(destBytes);
    if (cmsGetColorSpace(dest.get()) != cmsSigCmykData) fail("profile_mismatch", "destination profile must be CMYK");
    const auto intent = intents.at(o.intent);
    if (!cmsIsIntentSupported(source.get(), intent, LCMS_USED_AS_INPUT) ||
        !cmsIsIntentSupported(dest.get(), intent, LCMS_USED_AS_OUTPUT))
        fail("unsupported_intent", "ICC profiles do not support requested transform intent");
    using Transform = std::unique_ptr<void, decltype(&cmsDeleteTransform)>;
    Transform transform(cmsCreateTransform(source.get(), im.channels == 4 ? TYPE_CMYK_8 : TYPE_RGB_8,
        dest.get(), TYPE_CMYK_8, intent, o.blackPointCompensation ? cmsFLAGS_BLACKPOINTCOMPENSATION : 0), cmsDeleteTransform);
    if (!transform) fail("transform_failed", "Little CMS could not create ICC transform");
    std::vector<unsigned char> output(pixelSize(im.width, im.height, 4));
    cmsDoTransform(transform.get(), im.pixels.data(), output.data(), im.width * im.height);
    auto sourceInfo = profileInfo(source.get(), sourceBytes); sourceInfo["origin"] = origin;
    im.pixels = std::move(output); im.channels = 4; im.icc = destBytes;
    writeNew(o.destination, encode(im, o.format, o.quality));
    return {{"protocol", 1}, {"width", im.width}, {"height", im.height}, {"channels", 4},
        {"colorSpace", "CMYK"}, {"format", o.format}, {"sourceProfile", sourceInfo},
        {"destinationProfile", profileInfo(dest.get(), destBytes.size())}, {"warnings", warnings}};
}
}
