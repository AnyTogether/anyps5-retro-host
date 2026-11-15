// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

#include <clocale>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

#include "json.h"
#include "paths.h"

namespace {

int failures = 0;

void Expect(bool condition, const char* what) {
  if (!condition) {
    std::fprintf(stderr, "FAIL: %s\n", what);
    ++failures;
  }
}

}  // namespace

int main() {
  using retro_host::ParseJson;
  Expect(ParseJson(R"({"type":"ping","id":1})").has_value(), "a plain object parses");
  Expect(!ParseJson(R"({"a":1,"a":2})"), "a duplicate key is refused");
  Expect(!ParseJson(R"({"a":{"b":1,"b":1}})"), "a nested duplicate key is refused");
  Expect(!ParseJson(R"({"a":"\ud800"})"), "a lone high surrogate is refused");
  Expect(!ParseJson(R"({"a":"\udc00"})"), "a lone low surrogate is refused");
  Expect(ParseJson(R"({"a":"\ud83d\ude00"})").has_value(), "a surrogate pair parses");
  Expect(!ParseJson("{\"a\":\"\xff\"}"), "invalid UTF-8 is refused");
  Expect(!ParseJson("{\"a\":\"\xed\xa0\x80\"}"), "an encoded surrogate is refused");
  Expect(!ParseJson(R"({"a":1e400})"), "a number that is not finite is refused");
  Expect(!ParseJson(R"({"a":NaN})"), "NaN is refused");
  Expect(!ParseJson(R"({"a":01})"), "a leading zero is refused");
  Expect(!ParseJson(R"({"a":1} x)"), "trailing text is refused");
  const auto one = ParseJson(R"({"a":1.0,"b":1e0,"c":1.5})");
  Expect(one && one->Find("a")->Integer(1, 1) == 1, "1.0 is the integer 1");
  Expect(one && one->Find("b")->Integer(1, 1) == 1, "1e0 is the integer 1");
  Expect(one && !one->Find("c")->Integer(0, 2), "1.5 is not an integer");
  const auto text = ParseJson(R"({"s":"a\"b\\c\n\u00e9"})");
  Expect(text && text->Serialize() == "{\"s\":\"a\\\"b\\\\c\\n\xc3\xa9\"}", "strings round-trip");

  using retro_host::ClipUtf8;
  using retro_host::Json;
  using retro_host::ValidUtf8;
  const std::string bad = "\xef\xbf\xbd";
  Expect(Json(std::string("Disc \xff" "2")).Serialize() == "\"Disc " + bad + "2\"",
         "the writer sends a stray 0xFF byte as U+FFFD");
  Expect(Json(retro_host::JsonObject{{"Spi\xe9l", "Spi\xe9l"}}).Serialize() == "{\"Spi" + bad + "l\":\"Spi" + bad + "l\"}",
         "the writer cleans keys as well as values");
  const std::string mixed = "\xc3\x28\xe0\x80\xed\xa0\x80\xf0\x9f\x98!\xc0\xaf\xf4\x90\x80\x80\x80\xc3";
  Expect(ParseJson("{\"a\":" + Json(mixed).Serialize() + "}").has_value(), "what the writer sends parses");
  Expect(ValidUtf8("\xc3\xa9\xf0\x9f\x98\x80") == "\xc3\xa9\xf0\x9f\x98\x80", "valid UTF-8 is kept");
  Expect(ValidUtf8("Spi\xe9l") == "Spi" + bad + "l", "a Latin-1 byte is one U+FFFD");
  Expect(ValidUtf8("\xe0\x80") == bad + bad, "a lead byte the next byte cannot follow is its own U+FFFD");
  Expect(ValidUtf8("\xed\xa0\x80") == bad + bad + bad, "an encoded surrogate is three U+FFFD");
  Expect(ValidUtf8("\xf0\x9f\x98!") == bad + "!", "a character cut short is one U+FFFD");
  Expect(ValidUtf8("\xc0\xaf") == bad + bad, "an overlong form is one U+FFFD a byte");
  Expect(ValidUtf8("\xf4\x90\x80\x80") == bad + bad + bad + bad, "a value past U+10FFFF is one U+FFFD a byte");
  Expect(ValidUtf8("a\xc3") == "a" + bad, "a lead byte at the end is one U+FFFD");
  Expect(ClipUtf8("a\xc3\xa9", 2) == "a", "clipping keeps whole characters");
  Expect(ClipUtf8("ab\xff", 3) == "ab", "clipping cleans first, then keeps whole characters");
  Expect(ClipUtf8("\xff", 3) == bad, "a clipped U+FFFD that fits is kept");

  const char* const comma_locales[] = {"de_DE.UTF-8", "de_DE.utf8", "de_DE", "German_Germany.1252", "de-DE"};
  const char* comma = nullptr;
  for (const char* name : comma_locales) {
    if (std::setlocale(LC_NUMERIC, name) && std::strcmp(std::localeconv()->decimal_point, ",") == 0) {
      comma = name;
      break;
    }
  }
  if (comma) {
    const auto half = ParseJson(R"({"a":1.5})");
    Expect(half && half->Find("a")->AsNumber() == 1.5, "a number reads with a '.' under a decimal-comma locale");
    Expect(Json(4.0 / 3.0).Serialize() == "1.3333333333333333", "a number writes with a '.' under a decimal-comma locale");
    std::setlocale(LC_NUMERIC, "C");
  } else if (std::getenv("CI")) {
    Expect(false, "CI has a locale with a decimal comma (de_DE)");
  } else {
    std::printf("No locale with a decimal comma here, so the locale checks did not run.\n");
  }

  using retro_host::IsProtocolPath;
  Expect(IsProtocolPath("/home/a/b.cue", false), "a POSIX absolute path");
  Expect(!IsProtocolPath("/home/../b.cue", false), "a POSIX path with ..");
  Expect(!IsProtocolPath("/a//b", false), "a POSIX path with an empty segment");
  Expect(!IsProtocolPath("C:/a.cue", false), "a Windows path on POSIX");
  Expect(IsProtocolPath(R"(C:\Games\a.cue)", true), "a Windows drive path");
  Expect(IsProtocolPath("C:/Games/a.cue", true), "a Windows drive path with slashes");
  Expect(IsProtocolPath(R"(\\nas\games\a.cue)", true), "a UNC path");
  Expect(IsProtocolPath(R"(\\?\C:\Games\a.cue)", true), "a long drive path");
  Expect(IsProtocolPath(R"(\\?\UNC\nas\games\a.cue)", true), "a long UNC path");
  Expect(!IsProtocolPath(R"(\\?\C:)", true), "a long drive with no root");
  Expect(!IsProtocolPath(R"(\\nas\..\a.cue)", true), "a UNC share of ..");
  Expect(!IsProtocolPath(R"(\\.\PhysicalDrive0)", true), "a device path");
  Expect(!IsProtocolPath("C:a.cue", true), "a drive-relative path");
  Expect(!IsProtocolPath(R"(\a.cue)", true), "a root-relative path");
  Expect(!IsProtocolPath(std::string("/a\0b", 4), false), "a path with NUL");
  Expect(retro_host::HasExtension("/a/Game.CUE", ".cue"), "extensions ignore case");

  if (failures == 0) std::printf("All JSON and path checks passed.\n");
  return failures == 0 ? 0 : 1;
}
