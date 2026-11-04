#pragma once
#include <cstdint>
#include <filesystem>
#include <stdexcept>
#include <string>
#include <vector>
#include <nlohmann/json.hpp>

namespace image_cmyk {
using Json = nlohmann::json;
struct Error : std::runtime_error {
    std::string code;
    Error(std::string code, std::string message);
};
struct Image {
    std::uint32_t width = 0, height = 0;
    unsigned channels = 0, orientation = 1;
    double dpiX = 0, dpiY = 0;
    std::string format;
    std::vector<unsigned char> pixels, icc;
};
struct Options {
    std::filesystem::path source, destination, destinationProfile, sourceProfile;
    bool assumeSRGB = false, blackPointCompensation = true;
    std::string intent = "relativeColorimetric", format = "tiff";
    int quality = 90;
};
bool validUtf8(const std::string&);
std::vector<unsigned char> readFile(const std::filesystem::path&, std::size_t limit);
Image decode(const std::filesystem::path&);
Json inspect(const std::filesystem::path&);
Json convert(const Options&);
std::vector<unsigned char> encode(const Image&, const std::string&, int quality = 90);
void writeNew(const std::filesystem::path&, const std::vector<unsigned char>&);
}
