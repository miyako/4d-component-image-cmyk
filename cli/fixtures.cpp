#include "fixture_support.hpp"
#include <iostream>
#ifdef _WIN32
#define NOMINMAX
#include <windows.h>
#endif
namespace {
int generate(const std::filesystem::path& path) {
    try { image_cmyk::fixtures::make(path); return 0; }
    catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 2; }
}
}
#ifdef _WIN32
int wmain(int argc, wchar_t* argv[]) {
    if (argc != 2) return 2;
    return generate(std::filesystem::path(argv[1]));
}
#else
int main(int argc, char* argv[]) {
    if (argc != 2 || !image_cmyk::validUtf8(argv[1])) return 2;
    return generate(std::filesystem::u8path(argv[1]));
}
#endif
