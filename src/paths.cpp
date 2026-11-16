// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

#include "paths.h"

#include <cctype>
#include <vector>

namespace retro_host {
namespace {

bool IsLetter(char c) { return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z'); }

bool IsCSpace(char c) { return c == ' ' || (c >= '\t' && c <= '\r'); }

std::vector<std::string_view> Split(std::string_view text, bool slash_too) {
  std::vector<std::string_view> parts;
  size_t start = 0;
  for (size_t at = 0; at <= text.size(); ++at) {
    if (at == text.size() || text[at] == '\\' || (slash_too && text[at] == '/')) {
      parts.push_back(text.substr(start, at - start));
      start = at + 1;
    }
  }
  return parts;
}

bool GoodSegments(const std::vector<std::string_view>& parts) {
  for (const auto part : parts) {
    if (part.empty() || part == "." || part == "..") return false;
  }
  return true;
}

bool WindowsPath(std::string_view path) {
  const bool long_form = path.substr(0, 4) == "\\\\?\\";
  if (long_form) {
    std::string_view rest = path.substr(4);
    if (rest.substr(0, 4) == "UNC\\") {
      const auto parts = Split(rest.substr(4), false);
      return parts.size() >= 2 && GoodSegments(parts);
    }
    if (rest.size() < 3 || !IsLetter(rest[0]) || rest[1] != ':' || rest[2] != '\\') return false;
    if (rest.size() == 3) return true;
    return GoodSegments(Split(rest.substr(3), false));
  }
  const auto separator = [](char c) { return c == '\\' || c == '/'; };
  if (path.size() >= 2 && separator(path[0]) && separator(path[1])) {
    if (path.size() > 2 && (path[2] == '?' || path[2] == '.')) return false;
    const auto parts = Split(path.substr(2), true);
    return parts.size() >= 2 && GoodSegments(parts);
  }
  if (path.size() < 3 || !IsLetter(path[0]) || path[1] != ':' || !separator(path[2])) return false;
  if (path.size() == 3) return true;
  return GoodSegments(Split(path.substr(3), true));
}

bool PosixPath(std::string_view path) {
  if (path.empty() || path[0] != '/' || (path.size() > 1 && path[1] == '/')) return false;
  if (path.size() == 1) return true;
  std::vector<std::string_view> parts;
  size_t start = 1;
  for (size_t at = 1; at <= path.size(); ++at) {
    if (at == path.size() || path[at] == '/') {
      parts.push_back(path.substr(start, at - start));
      start = at + 1;
    }
  }
  return GoodSegments(parts);
}

}  // namespace

bool IsProtocolPath(std::string_view path, bool windows) {
  if (path.find('\0') != std::string_view::npos) return false;
  return windows ? WindowsPath(path) : PosixPath(path);
}

bool IsCoreAbsolute(std::string_view name, bool windows) {
  if (!windows) return !name.empty() && name[0] == '/';
  return name.size() >= 3 && IsLetter(name[0]) && name[1] == ':' && (name[2] == '/' || name[2] == '\\');
}

std::optional<std::string> PlaylistEntry(std::string_view m3u_path, std::string_view text, unsigned index,
                                         bool windows) {
  unsigned at = 0;
  size_t start = 0;
  while (start < text.size()) {
    size_t end = text.find('\n', start);
    if (end == std::string_view::npos) end = text.size();
    const std::string_view line = text.substr(start, end - start);
    start = end + 1;
    size_t first = 0;
    while (first < line.size() && IsCSpace(line[first])) ++first;
    if (first == line.size() || line[first] == '#') continue;
    size_t last = line.size() - 1;
    while (IsCSpace(line[last]) && last > first) --last;
    if (first == last) continue;
    if (at++ != index) continue;
    const std::string_view name = line.substr(first, last - first + 1);
    if (IsCoreAbsolute(name, windows)) return std::string(name);
    const size_t slash = m3u_path.find_last_of(windows ? "/\\" : "/");
    std::string path(slash == std::string_view::npos ? std::string_view() : m3u_path.substr(0, slash + 1));
    return path.append(name);
  }
  return std::nullopt;
}

bool HasExtension(std::string_view path, std::string_view extension) {
  if (path.size() < extension.size()) return false;
  const auto tail = path.substr(path.size() - extension.size());
  for (size_t i = 0; i < extension.size(); ++i) {
    if (std::tolower(static_cast<unsigned char>(tail[i])) != std::tolower(static_cast<unsigned char>(extension[i]))) {
      return false;
    }
  }
  return true;
}

}  // namespace retro_host
