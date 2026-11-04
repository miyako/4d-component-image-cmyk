#include "fixture_support.hpp"
#include <cstdio>
#include <fstream>
#include <functional>
#include <iostream>
#include <jpeglib.h>
#include <tiffio.h>
#ifdef _WIN32
#include <process.h>
#else
#include <unistd.h>
#endif

namespace {
using namespace image_cmyk;
unsigned checks = 0;
void check(bool value, const char* message) {
    ++checks; if (!value) throw std::runtime_error(message);
}
void error(const std::string& code, const std::function<void()>& f) {
    try { f(); } catch (const Error& e) { check(e.code == code, (code + " expected, got " + e.code + ": " + e.what()).c_str()); return; }
    throw std::runtime_error("expected error: " + code);
}
std::vector<unsigned char> changeTiff(std::vector<unsigned char> b, unsigned tag, unsigned value) {
    const bool le = b[0] == 'I';
    auto read = [&](std::size_t o, unsigned n) {
        unsigned v = 0;
        for (unsigned i = 0; i < n; ++i) v |= unsigned(b[o + i]) << (8 * (le ? i : n - 1 - i));
        return v;
    };
    auto write = [&](std::size_t o, unsigned n, unsigned v) {
        for (unsigned i = 0; i < n; ++i) b[o + i] = (v >> (8 * (le ? i : n - 1 - i))) & 255;
    };
    const auto offset = read(4, 4), count = read(offset, 2);
    for (unsigned i = 0; i < count; ++i) {
        const auto o = offset + 2 + i * 12;
        if (read(o, 2) != tag) continue;
        auto type = read(o + 2, 2);
        const auto num = read(o + 4, 4);
        check(num == 1, "test TIFF field must be scalar");
        if (type == 3 && value > 65535) { type = 4; write(o + 2, 2, type); }
        write(o + 8, type == 3 ? 2 : 4, value); return b;
    }
    throw std::runtime_error("missing test TIFF field");
}
std::vector<unsigned char> multipage(std::vector<unsigned char> b) {
    const bool le = b[0] == 'I';
    auto read = [&](std::size_t o, unsigned n) {
        unsigned v = 0;
        for (unsigned i = 0; i < n; ++i) v |= unsigned(b[o + i]) << (8 * (le ? i : n - 1 - i));
        return v;
    };
    const auto offset = read(4, 4), count = read(offset, 2), size = 2 + count * 12 + 4;
    const auto next = static_cast<unsigned>(b.size());
    std::vector<unsigned char> directory(b.begin() + offset, b.begin() + offset + size);
    b.insert(b.end(), directory.begin(), directory.end());
    for (unsigned i = 0; i < 4; ++i) b[offset + size - 4 + i] = (next >> (8 * (le ? i : 3 - i))) & 255;
    return b;
}
std::vector<unsigned char> jpegCMYK(const Image& im, bool adobe, bool ycck) {
    jpeg_compress_struct c{}; jpeg_error_mgr errors{};
    c.err = jpeg_std_error(&errors); jpeg_create_compress(&c);
    unsigned char* bytes = nullptr; unsigned long n = 0;
    jpeg_mem_dest(&c, &bytes, &n);
    c.image_width = im.width; c.image_height = im.height; c.input_components = 4; c.in_color_space = JCS_CMYK;
    jpeg_set_defaults(&c);
    if (ycck) jpeg_set_colorspace(&c, JCS_YCCK);
    jpeg_set_quality(&c, 100, TRUE);
    for (int i = 0; i < c.num_components; ++i) { c.comp_info[i].h_samp_factor = 1; c.comp_info[i].v_samp_factor = 1; }
    c.write_Adobe_marker = adobe;
    auto pixels = im.pixels;
    if (adobe) for (auto& v : pixels) v = 255 - v;
    jpeg_start_compress(&c, TRUE);
    while (c.next_scanline < c.image_height) {
        JSAMPROW row = pixels.data() + std::size_t(c.next_scanline) * im.width * 4;
        jpeg_write_scanlines(&c, &row, 1);
    }
    jpeg_finish_compress(&c);
    std::vector<unsigned char> result(bytes, bytes + n);
    jpeg_destroy_compress(&c); std::free(bytes); return result;
}
void test(const std::filesystem::path& dir) {
    fixtures::make(dir);
    auto im = fixtures::rgb();
    const auto destination = readFile(dir / "destination.icc", 32u * 1024u * 1024u);
    check(destination == fixtures::cmykProfile(), "fixture ICC must be deterministic");
    for (const auto* name : {"input.png", "input.tif", "input.jpg"}) {
        const auto info = inspect(dir / name);
        check(info["width"] == 8 && info["height"] == 6 && info["channels"] == 3, "RGB inspection dimensions/channels");
        check(info["colorSpace"] == "RGB" && info["bitDepth"] == 8 && info["alpha"] == false, "RGB inspection schema");
        check(info["embeddedProfile"]["present"] == true && info["embeddedProfile"]["colorSpace"] == "RGB", "embedded RGB ICC");
        check(std::abs(info["dpiX"].get<double>() - 144) < .02 && std::abs(info["dpiY"].get<double>() - 72) < .02, "input DPI preservation");
    }
    const auto output = decode(dir / "cmyk.tif");
    check(output.channels == 4 && output.icc == destination, "exact output ICC and CMYK channels");
    const auto untaggedCMYK = inspect(dir / "untagged-cmyk.tif");
    check(untaggedCMYK["channels"] == 4 && untaggedCMYK["embeddedProfile"]["present"] == false, "untagged CMYK fixture");
    check(output.dpiX == 144 && output.dpiY == 72, "TIFF DPI preserved");
    auto src = cmsOpenProfileFromMem(im.icc.data(), static_cast<cmsUInt32Number>(im.icc.size()));
    auto dst = cmsOpenProfileFromMem(destination.data(), static_cast<cmsUInt32Number>(destination.size()));
    check(src && dst, "reference profiles open");
    auto transform = cmsCreateTransform(src, TYPE_RGB_8, dst, TYPE_CMYK_8, INTENT_RELATIVE_COLORIMETRIC, cmsFLAGS_BLACKPOINTCOMPENSATION);
    check(transform != nullptr, "independent lcms reference transform");
    std::vector<unsigned char> reference(8 * 6 * 4);
    cmsDoTransform(transform, im.pixels.data(), reference.data(), 48);
    cmsDeleteTransform(transform); cmsCloseProfile(src); cmsCloseProfile(dst);
    check(output.pixels == reference, "TIFF pixels must exactly match independently invoked lcms transform");
    const auto jpeg = decode(dir / "cmyk.jpg");
    check(jpeg.icc == destination && jpeg.channels == 4 && jpeg.orientation == 1, "JPEG output ICC/channel/orientation");
    check(std::abs(jpeg.dpiX - 144) < .0001 && std::abs(jpeg.dpiY - 72) < .0001, "CMYK JPEG EXIF DPI");
    unsigned maxDifference = 0;
    for (std::size_t i = 0; i < reference.size(); ++i)
        maxDifference = std::max(maxDifference, unsigned(std::abs(int(jpeg.pixels[i]) - reference[i])));
    check(maxDifference < 5, "Adobe CMYK JPEG inversion must preserve colors");
    Options o; o.source = dir / "input.tif"; o.destinationProfile = dir / "destination.icc";
    o.destination = dir / "repeat.tif";
    const auto result = convert(o);
    check(result["sourceProfile"]["origin"] == "embedded" && result["warnings"].empty(), "embedded source provenance");
    for (const auto* name : {"cmyk.tif", "cmyk.jpg"}) {
        auto cmyk = o; cmyk.source = dir / name; cmyk.destination = dir / (std::string(name) + "-converted.tif");
        check(convert(cmyk)["sourceProfile"]["colorSpace"] == "CMYK", "embedded CMYK ICC source transform");
    }
    error("destination_exists", [&] { convert(o); });
    const auto original = readFile(o.source, 1000000);
    o.destination = o.source;
    error("destination_exists", [&] { convert(o); });
    check(readFile(o.source, 1000000) == original, "source preserved");
    const auto alias = dir / "source-hardlink.tif";
    std::filesystem::create_hard_link(o.source, alias); o.destination = alias;
    error("destination_exists", [&] { convert(o); });
    check(readFile(o.source, 1000000) == original, "hardlink source preserved");
    std::error_code ec;
    const auto symlink = dir / "source-symlink.tif";
    std::filesystem::create_symlink(std::filesystem::absolute(o.source), symlink, ec);
    if (!ec) { o.destination = symlink; error("destination_exists", [&] { convert(o); }); }
    o.destination = dir / "override.tif"; o.sourceProfile = dir / "source.icc";
    error("profile_override", [&] { convert(o); });
    o.source = dir / "untagged.png"; o.sourceProfile.clear();
    error("missing_source_profile", [&] { convert(o); });
    o.assumeSRGB = true;
    auto assumed = convert(o);
    check(assumed["sourceProfile"]["origin"] == "assumed-srgb" && assumed["warnings"].size() == 1, "explicit sRGB assumption warning");
    o.destination = dir / "supplied.tif"; o.assumeSRGB = false; o.sourceProfile = dir / "source.icc";
    check(convert(o)["sourceProfile"]["origin"] == "supplied", "explicit untagged source profile");
    o.destination = dir / "mismatch.tif"; o.sourceProfile = dir / "destination.icc";
    error("profile_mismatch", [&] { convert(o); });
    o.sourceProfile = dir / "source.icc"; o.destinationProfile = dir / "source.icc";
    error("profile_mismatch", [&] { convert(o); });
    o.destinationProfile = dir / "destination.icc"; o.intent = "not-an-intent";
    error("invalid_arguments", [&] { convert(o); }); o.intent = "relativeColorimetric";
    o.quality = 0; error("invalid_arguments", [&] { convert(o); }); o.quality = 90;
    writeNew(dir / "malformed.icc", std::vector<unsigned char>(128, 0));
    o.sourceProfile = dir / "malformed.icc";
    error("invalid_profile", [&] { convert(o); });
    auto malformed = im; malformed.icc = std::vector<unsigned char>(128, 0);
    writeNew(dir / "malformed-tag.tif", encode(malformed, "tiff"));
    error("invalid_profile", [&] { inspect(dir / "malformed-tag.tif"); });
    writeNew(dir / "malformed-tag.png", fixtures::png(malformed));
    error("invalid_profile", [&] { inspect(dir / "malformed-tag.png"); });
    auto mismatched = im; mismatched.icc = destination;
    writeNew(dir / "wrong-colorspace.tif", encode(mismatched, "tiff"));
    error("profile_mismatch", [&] { inspect(dir / "wrong-colorspace.tif"); });
    error("unsupported_image", [&] { inspect(dir / "alpha.png"); });
    writeNew(dir / "bad.bin", {1, 2, 3});
    error("unsupported_image", [&] { inspect(dir / "bad.bin"); });
    auto truncated = readFile(dir / "input.jpg", 1000000); truncated.resize(truncated.size() - 10);
    writeNew(dir / "truncated.jpg", truncated);
    error("invalid_image", [&] { inspect(dir / "truncated.jpg"); });
    auto rotatedJpeg = readFile(dir / "input.jpg", 1000000);
    const std::string exifSignature("Exif\0\0II", 8);
    auto marker = std::search(rotatedJpeg.begin(), rotatedJpeg.end(), exifSignature.begin(), exifSignature.end());
    check(marker != rotatedJpeg.end(), "fixture EXIF marker exists");
    *(marker + 24) = 6;
    writeNew(dir / "orientation.jpg", rotatedJpeg);
    error("unsupported_orientation", [&] { inspect(dir / "orientation.jpg"); });
    auto tiff = encode(im, "tiff");
    writeNew(dir / "orientation-patched.tif", changeTiff(tiff, TIFFTAG_ORIENTATION, 6));
    error("unsupported_orientation", [&] { inspect(dir / "orientation-patched.tif"); });
    error("unsupported_orientation", [&] { inspect(dir / "orientation.tif"); });
    writeNew(dir / "planar.tif", changeTiff(tiff, TIFFTAG_PLANARCONFIG, PLANARCONFIG_SEPARATE));
    error("unsupported_image", [&] { inspect(dir / "planar.tif"); });
    writeNew(dir / "huge.tif", changeTiff(tiff, TIFFTAG_IMAGEWIDTH, 40000001));
    error("image_limit", [&] { inspect(dir / "huge.tif"); });
    writeNew(dir / "pages.tif", multipage(tiff));
    error("unsupported_image", [&] { inspect(dir / "pages.tif"); });
    writeNew(dir / "oversize", {});
    std::filesystem::resize_file(dir / "oversize", 256u * 1024u * 1024u + 1);
    error("input_limit", [&] { inspect(dir / "oversize"); });
    writeNew(dir / "oversize.icc", {});
    std::filesystem::resize_file(dir / "oversize.icc", 32u * 1024u * 1024u + 1);
    o = Options{}; o.source = dir / "input.tif"; o.destinationProfile = dir / "oversize.icc";
    o.destination = dir / "oversize-profile-output.tif";
    error("input_limit", [&] { convert(o); });
    auto flat = output; flat.width = 16; flat.height = 16; flat.icc.clear();
    flat.pixels.clear();
    for (unsigned i = 0; i < 256; ++i) flat.pixels.insert(flat.pixels.end(), {30, 70, 120, 50});
    for (const bool ycck : {false, true}) for (const bool adobe : {false, true}) {
        if (ycck && !adobe) continue;
        const auto path = dir / (std::string(ycck ? "ycck" : "cmyk") + (adobe ? "-adobe.jpg" : "-plain.jpg"));
        writeNew(path, jpegCMYK(flat, adobe, ycck));
        const auto decoded = decode(path);
        check(decoded.channels == 4, "CMYK/YCCK JPEG recognized");
        for (unsigned c = 0; c < 4; ++c)
            check(std::abs(int(decoded.pixels[c]) - flat.pixels[c]) <= 2, "CMYK/YCCK JPEG sample convention");
        o = Options{}; o.source = path; o.destination = path.u8string() + ".tif";
        o.destinationProfile = dir / "destination.icc";
        error("missing_source_profile", [&] { convert(o); });
        o.assumeSRGB = true;
        error("missing_source_profile", [&] { convert(o); });
        o.sourceProfile = dir / "destination.icc";
        check(convert(o)["sourceProfile"]["origin"] == "supplied", "CMYK/YCCK supplied ICC conversion");
    }
    o = Options{}; o.source = dir / "input.tif"; o.destinationProfile = dir / "destination.icc";
    for (const auto* intent : {"perceptual", "saturation", "absoluteColorimetric"}) {
        o.intent = intent; o.blackPointCompensation = false; o.destination = dir / (std::string(intent) + ".tif");
        check(convert(o)["channels"] == 4, "rendering intent/BPC option");
    }
    auto fractional = im; fractional.dpiX = 72.25; fractional.dpiY = 143.875;
    writeNew(dir / "fractional.jpg", encode(fractional, "jpeg", 90));
    const auto fractions = decode(dir / "fractional.jpg");
    check(std::abs(fractions.dpiX - fractional.dpiX) < .0001 && std::abs(fractions.dpiY - fractional.dpiY) < .0001, "fractional JPEG DPI");
    fractional.dpiX = 0; fractional.dpiY = 1000000;
    writeNew(dir / "one-axis.jpg", encode(fractional, "jpeg", 90));
    const auto oneAxis = decode(dir / "one-axis.jpg");
    check(oneAxis.dpiX == 0 && oneAxis.dpiY == 1000000, "single-axis and large JPEG DPI");
    for (const auto& entry : std::filesystem::directory_iterator(dir))
        check(entry.path().extension() != ".partial", "staging files cleaned");
    check(validUtf8(u8"日本語 👩🏽‍💻") && !validUtf8("\xc0\x80") && !validUtf8("\xed\xa0\x80"), "strict UTF8 validation");
}
}
int main() {
#ifdef _WIN32
    const auto pid = _getpid();
#else
    const auto pid = getpid();
#endif
    const auto dir = std::filesystem::current_path() / ("native-test-fixtures-" + std::to_string(pid));
    try {
        if (std::filesystem::exists(dir)) throw std::runtime_error("test fixture directory already exists");
        test(dir);
        std::filesystem::remove_all(dir);
        std::cout << checks << " native assertions passed\n"; return 0;
    } catch (const std::exception& e) {
        std::cerr << "Native test failed: " << e.what() << "\nEvidence retained at " << dir.u8string() << '\n'; return 1;
    }
}
