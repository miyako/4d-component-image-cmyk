#include "image.hpp"
#include <iostream>
#include <map>
#include <set>
#ifdef _WIN32
#define NOMINMAX
#include <windows.h>
#include <fcntl.h>
#include <io.h>
#endif

namespace {
using namespace image_cmyk;
int run(const std::vector<std::string>& args) {
    try {
        for (const auto& a : args)
            if (!validUtf8(a) || a.find('\0') != std::string::npos)
                throw Error("invalid_arguments", "arguments must be valid UTF-8 without NUL");
        if (args == std::vector<std::string>{"--version"}) {
            std::cout << Json({{"protocol", 1}, {"version", IMAGE_CMYK_VERSION}}).dump() << '\n';
            return 0;
        }
        if (args.empty() || (args[0] != "inspect" && args[0] != "convert"))
            throw Error("invalid_arguments", "expected inspect, convert or --version");
        const bool conversion = args[0] == "convert";
        const std::set<std::string> allowed = conversion
            ? std::set<std::string>{"--source", "--destination", "--destination-profile",
                "--source-profile", "--allow-assume-srgb", "--intent",
                "--black-point-compensation", "--format", "--quality"}
            : std::set<std::string>{"--source"};
        std::map<std::string, std::string> values;
        for (std::size_t i = 1; i < args.size(); i += 2) {
            if (!allowed.count(args[i]) || i + 1 == args.size() ||
                args[i + 1].empty() || args[i + 1].rfind("--", 0) == 0 ||
                !values.emplace(args[i], args[i + 1]).second)
                throw Error("invalid_arguments", "unknown, duplicate, empty or missing option");
        }
        auto required = [&](const std::string& key) {
            if (!values.count(key)) throw Error("invalid_arguments", "missing " + key);
            return std::filesystem::u8path(values.at(key));
        };
        auto boolean = [&](const std::string& key, bool fallback) {
            if (!values.count(key)) return fallback;
            if (values[key] != "true" && values[key] != "false")
                throw Error("invalid_arguments", key + " requires true or false");
            return values[key] == "true";
        };
        const auto source = required("--source");
        Json result;
        if (!conversion) result = inspect(source);
        else {
            Options o;
            o.source = source;
            o.destination = required("--destination");
            o.destinationProfile = required("--destination-profile");
            if (values.count("--source-profile")) o.sourceProfile = std::filesystem::u8path(values["--source-profile"]);
            o.assumeSRGB = boolean("--allow-assume-srgb", false);
            o.blackPointCompensation = boolean("--black-point-compensation", true);
            if (values.count("--intent")) o.intent = values["--intent"];
            if (values.count("--format")) o.format = values["--format"];
            if (values.count("--quality")) {
                const auto& q = values["--quality"];
                if (q.size() > 3 || q.find_first_not_of("0123456789") != std::string::npos)
                    throw Error("invalid_arguments", "quality must be an integer from 1 to 100");
                o.quality = std::stoi(q);
            }
            if ((o.format != "jpeg" && o.format != "tiff") || o.quality < 1 || o.quality > 100)
                throw Error("invalid_arguments", "format must be tiff or jpeg; quality must be 1..100");
            result = convert(o);
        }
        std::cout << result.dump() << '\n';
        return 0;
    } catch (const Error& e) {
        std::cerr << Json({{"protocol", 1}, {"error", {{"code", e.code}, {"message", e.what()}}}}).dump() << '\n';
    } catch (const std::exception&) {
        std::cerr << R"({"protocol":1,"error":{"code":"internal_error","message":"operation failed"}})" << '\n';
    }
    return 2;
}
}
#ifdef _WIN32
int wmain(int argc, wchar_t* argv[]) {
    _setmode(_fileno(stdout), _O_BINARY);
    _setmode(_fileno(stderr), _O_BINARY);
    std::vector<std::string> args;
    for (int i = 1; i < argc; ++i) {
        const int n = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, argv[i], -1, nullptr, 0, nullptr, nullptr);
        if (!n) return run({"\xff"});
        std::string s(n, '\0');
        if (!WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, argv[i], -1, s.data(), n, nullptr, nullptr))
            return run({"\xff"});
        s.pop_back();
        args.push_back(std::move(s));
    }
    return run(args);
}
#else
int main(int argc, char* argv[]) { return run(std::vector<std::string>(argv + 1, argv + argc)); }
#endif
