// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

#ifndef ANYPS5_RETRO_HOST_PATHS_H_
#define ANYPS5_RETRO_HOST_PATHS_H_

#include <optional>
#include <string>
#include <string_view>

namespace retro_host {

// A path as protocol.md "Paths" allows it for this system: absolute, with no NUL and no ".",
// ".." or empty segment.
bool IsProtocolPath(std::string_view path, bool windows);

// Whether SwanStation opens a name listed in a .cue or .m3u as written rather than joining it to
// the listing file's directory: on Windows a drive path (C:\a.bin or C:/a.bin), elsewhere a name
// that starts with /. This is SwanStation's FileSystem::IsAbsolutePath at the commit the host
// is tested with (protocol.md "Paths").
bool IsCoreAbsolute(std::string_view name, bool windows);

// The file SwanStation opens for entry index of the .m3u at m3u_path, whose contents are text, or
// nothing when the playlist has no such entry. The entries are counted as cd_image_m3u.cpp counts
// them at that commit: each line (split at \n) loses its leading and trailing C-locale whitespace
// (space, \t, \n, \v, \f, \r); a line left empty, starting with #, or of one character is no
// entry; a byte order mark is not removed, so it is part of the first line. A name IsCoreAbsolute
// takes is returned as written; any other is joined to m3u_path's directory, as BuildRelativePath
// joins it.
std::optional<std::string> PlaylistEntry(std::string_view m3u_path, std::string_view text, unsigned index,
                                         bool windows);

// Whether path ends with extension (".cue"), ignoring ASCII case.
bool HasExtension(std::string_view path, std::string_view extension);

}  // namespace retro_host

#endif  // ANYPS5_RETRO_HOST_PATHS_H_
