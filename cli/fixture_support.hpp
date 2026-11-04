#pragma once
#include "image.hpp"
#include <algorithm>
#include <array>
#include <cmath>
#include <cstring>
#include <memory>
#include <lcms2.h>
#include <png.h>
#include <zlib.h>

namespace image_cmyk::fixtures {
inline void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}
inline std::vector<unsigned char> save(cmsHPROFILE p) {
    cmsUInt32Number n = 0;
    require(p && cmsSaveProfileToMem(p, nullptr, &n), "fixture profile sizing failed");
    std::vector<unsigned char> bytes(n);
    require(cmsSaveProfileToMem(p, bytes.data(), &n), "fixture profile serialization failed");
    // Fixed ICC creation date makes the test fixture byte-reproducible.
    const unsigned char date[] = {7, 228, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0};
    std::copy(std::begin(date), std::end(date), bytes.begin() + 24);
    return bytes;
}
struct Sampler {
    cmsHTRANSFORM rgbToLab = nullptr, labToRgb = nullptr;
    ~Sampler() { if (rgbToLab) cmsDeleteTransform(rgbToLab); if (labToRgb) cmsDeleteTransform(labToRgb); }
};
inline int forward(const cmsUInt16Number in[], cmsUInt16Number out[], void* cargo) {
    auto& s = *static_cast<Sampler*>(cargo);
    double rgb[3];
    for (unsigned i = 0; i < 3; ++i) rgb[i] = (1.0 - in[i] / 65535.0) * (1.0 - in[3] / 65535.0);
    cmsDoTransform(s.rgbToLab, rgb, out, 1);
    return 1;
}
inline int reverse(const cmsUInt16Number in[], cmsUInt16Number out[], void* cargo) {
    auto& s = *static_cast<Sampler*>(cargo);
    double rgb[3];
    cmsDoTransform(s.labToRgb, in, rgb, 1);
    for (auto& v : rgb) v = std::clamp(v, 0.0, 1.0);
    const double k = 1.0 - std::max({rgb[0], rgb[1], rgb[2]});
    for (unsigned i = 0; i < 3; ++i)
        out[i] = static_cast<cmsUInt16Number>(std::lround((k >= 1 ? 0 : (1 - rgb[i] - k) / (1 - k)) * 65535));
    out[3] = static_cast<cmsUInt16Number>(std::lround(k * 65535));
    return 1;
}
inline std::vector<unsigned char> cmykProfile() {
    using P = std::unique_ptr<void, decltype(&cmsCloseProfile)>;
    using Pipeline = std::unique_ptr<cmsPipeline, decltype(&cmsPipelineFree)>;
    P rgb(cmsCreate_sRGBProfile(), cmsCloseProfile), lab(cmsCreateLab4Profile(nullptr), cmsCloseProfile);
    Sampler s;
    s.rgbToLab = cmsCreateTransform(rgb.get(), TYPE_RGB_DBL, lab.get(), TYPE_Lab_16, INTENT_RELATIVE_COLORIMETRIC, cmsFLAGS_NOOPTIMIZE);
    s.labToRgb = cmsCreateTransform(lab.get(), TYPE_Lab_16, rgb.get(), TYPE_RGB_DBL, INTENT_RELATIVE_COLORIMETRIC, cmsFLAGS_NOOPTIMIZE);
    require(s.rgbToLab && s.labToRgb, "fixture sampler transform failed");
    P p(cmsCreateProfilePlaceholder(nullptr), cmsCloseProfile);
    require(bool(p), "fixture profile allocation failed");
    cmsSetProfileVersion(p.get(), 2.1);
    cmsSetDeviceClass(p.get(), cmsSigOutputClass);
    cmsSetColorSpace(p.get(), cmsSigCmykData); cmsSetPCS(p.get(), cmsSigLabData);
    cmsSetHeaderRenderingIntent(p.get(), INTENT_RELATIVE_COLORIMETRIC);
    cmsMLU* text = cmsMLUalloc(nullptr, 1);
    require(text != nullptr, "fixture text allocation failed");
    cmsMLUsetASCII(text, "en", "US", "Synthetic idealized CMYK plumbing fixture; NOT a print characterization");
    bool ok = cmsWriteTag(p.get(), cmsSigProfileDescriptionTag, text);
    cmsMLUsetASCII(text, "en", "US", "Original test fixture, MIT");
    ok = ok && cmsWriteTag(p.get(), cmsSigCopyrightTag, text);
    cmsMLUfree(text);
    ok = ok && cmsWriteTag(p.get(), cmsSigMediaWhitePointTag, cmsD50_XYZ());
    require(ok, "fixture profile tags failed");
    auto pipeline = [&](unsigned input, unsigned output, cmsSAMPLER16 sampler) {
        Pipeline pipe(cmsPipelineAlloc(nullptr, input, output), cmsPipelineFree);
        require(bool(pipe), "fixture pipeline allocation failed");
        cmsToneCurve* curves[4] = {};
        for (auto& curve : curves) curve = cmsBuildGamma(nullptr, 1);
        cmsStage* first = cmsStageAllocToneCurves(nullptr, input, curves);
        cmsStage* last = cmsStageAllocToneCurves(nullptr, output, curves);
        for (auto curve : curves) cmsFreeToneCurve(curve);
        cmsStage* clut = cmsStageAllocCLut16bit(nullptr, input == 4 ? 9 : 17, input, output, nullptr);
        require(first && last && clut, "fixture stage allocation failed");
        require(cmsStageSampleCLut16bit(clut, sampler, &s, 0), "fixture CLUT sampling failed");
        require(cmsPipelineInsertStage(pipe.get(), cmsAT_END, first), "fixture curve insertion failed");
        require(cmsPipelineInsertStage(pipe.get(), cmsAT_END, clut), "fixture CLUT insertion failed");
        require(cmsPipelineInsertStage(pipe.get(), cmsAT_END, last), "fixture curve insertion failed");
        return pipe;
    };
    auto a = pipeline(4, 3, forward), b = pipeline(3, 4, reverse);
    require(cmsWriteTag(p.get(), cmsSigAToB0Tag, a.get()) && cmsWriteTag(p.get(), cmsSigBToA0Tag, b.get()), "fixture LUT tags failed");
    require(cmsLinkTag(p.get(), cmsSigAToB1Tag, cmsSigAToB0Tag) &&
        cmsLinkTag(p.get(), cmsSigAToB2Tag, cmsSigAToB0Tag) &&
        cmsLinkTag(p.get(), cmsSigBToA1Tag, cmsSigBToA0Tag) &&
        cmsLinkTag(p.get(), cmsSigBToA2Tag, cmsSigBToA0Tag), "fixture intent links failed");
    return save(p.get());
}
inline std::vector<unsigned char> srgbProfile() {
    auto p = cmsCreate_sRGBProfile(); require(p != nullptr, "sRGB creation failed");
    auto result = save(p); cmsCloseProfile(p); return result;
}
inline void chunk(std::vector<unsigned char>& out, const char* type, const std::vector<unsigned char>& payload) {
    auto u32 = [&](std::uint32_t v) { for (int i = 3; i >= 0; --i) out.push_back((v >> (8 * i)) & 255); };
    u32(static_cast<std::uint32_t>(payload.size()));
    const auto start = out.size();
    out.insert(out.end(), type, type + 4); out.insert(out.end(), payload.begin(), payload.end());
    u32(static_cast<std::uint32_t>(crc32(0, out.data() + start, static_cast<uInt>(payload.size() + 4))));
}
inline std::vector<unsigned char> png(const Image& im, bool alpha = false) {
    png_image p{}; p.version = PNG_IMAGE_VERSION; p.width = im.width; p.height = im.height;
    p.format = alpha ? PNG_FORMAT_RGBA : PNG_FORMAT_RGB;
    std::vector<unsigned char> pixels;
    if (alpha) {
        for (std::size_t i = 0; i < im.pixels.size(); i += 3) {
            pixels.insert(pixels.end(), im.pixels.begin() + i, im.pixels.begin() + i + 3); pixels.push_back(128);
        }
    } else pixels = im.pixels;
    png_alloc_size_t size = 0;
    require(png_image_write_to_memory(&p, nullptr, &size, 0, pixels.data(), 0, nullptr), "PNG sizing failed");
    std::vector<unsigned char> bytes(size);
    require(png_image_write_to_memory(&p, bytes.data(), &size, 0, pixels.data(), 0, nullptr), "PNG fixture encoding failed");
    bytes.resize(size); png_image_free(&p);
    std::vector<unsigned char> extra;
    if (!im.icc.empty()) {
        uLongf n = compressBound(static_cast<uLong>(im.icc.size()));
        std::vector<unsigned char> compressed(n);
        require(compress2(compressed.data(), &n, im.icc.data(), static_cast<uLong>(im.icc.size()), 9) == Z_OK, "ICC compression failed");
        std::vector<unsigned char> payload{'I','C','C',0,0};
        payload.insert(payload.end(), compressed.begin(), compressed.begin() + n);
        chunk(extra, "iCCP", payload);
    }
    if (im.dpiX > 0 && im.dpiY > 0) {
        std::vector<unsigned char> payload;
        for (auto v : {static_cast<std::uint32_t>(std::lround(im.dpiX / .0254)), static_cast<std::uint32_t>(std::lround(im.dpiY / .0254))})
            for (int i = 3; i >= 0; --i) payload.push_back((v >> (8 * i)) & 255);
        payload.push_back(1); chunk(extra, "pHYs", payload);
    }
    // IHDR occupies the initial 33 bytes; ancillary color metadata precedes IDAT.
    bytes.insert(bytes.begin() + 33, extra.begin(), extra.end());
    return bytes;
}
inline Image rgb() {
    Image im; im.width = 8; im.height = 6; im.channels = 3; im.dpiX = 144; im.dpiY = 72;
    im.icc = srgbProfile();
    for (unsigned y = 0; y < im.height; ++y) for (unsigned x = 0; x < im.width; ++x) {
        im.pixels.push_back(static_cast<unsigned char>(x * 255 / 7));
        im.pixels.push_back(static_cast<unsigned char>(y * 255 / 5));
        im.pixels.push_back(static_cast<unsigned char>((x + y) * 255 / 12));
    }
    return im;
}
inline void make(const std::filesystem::path& directory) {
    std::filesystem::create_directories(directory);
    const auto im = rgb();
    const auto destination = cmykProfile();
    writeNew(directory / "source.icc", im.icc);
    writeNew(directory / "destination.icc", destination);
    writeNew(directory / "input.png", png(im));
    writeNew(directory / "input.tif", encode(im, "tiff"));
    writeNew(directory / "input.jpg", encode(im, "jpeg", 100));
    auto untagged = im; untagged.icc.clear();
    writeNew(directory / "untagged.png", png(untagged));
    writeNew(directory / "alpha.png", png(im, true));
    auto oriented = im; oriented.orientation = 6;
    writeNew(directory / "orientation.tif", encode(oriented, "tiff"));
    Options o; o.source = directory / "input.tif"; o.destination = directory / "cmyk.tif";
    o.destinationProfile = directory / "destination.icc"; convert(o);
    auto untaggedCMYK = decode(directory / "cmyk.tif"); untaggedCMYK.icc.clear();
    writeNew(directory / "untagged-cmyk.tif", encode(untaggedCMYK, "tiff"));
    o.destination = directory / "cmyk.jpg"; o.format = "jpeg"; o.quality = 100; convert(o);
    const Json expected = {{"width", im.width}, {"height", im.height}, {"dpiX", im.dpiX}, {"dpiY", im.dpiY},
        {"fixture", "synthetic ICC pipeline; not a print characterization"}};
    const auto text = expected.dump() + "\n";
    writeNew(directory / "expected.json", {text.begin(), text.end()});
}
}
