/* sane - Scanner Access Now Easy.

   Copyright (C) 2026 Christian Gillinger

   This file is part of the SANE package.

   This program is free software; you can redistribute it and/or
   modify it under the terms of the GNU General Public License as
   published by the Free Software Foundation; either version 2 of the
   License, or (at your option) any later version.

   This program is distributed in the hope that it will be useful, but
   WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
   General Public License for more details.

   You should have received a copy of the GNU General Public License
   along with this program.  If not, see <https://www.gnu.org/licenses/>.
*/

/* GL126 registers the backend addresses by name.

   Only the registers whose meaning is established from the captures are
   named here (docs/protocol-notes.md); the rest are written as opaque
   values from the generated tables in gl126_tables.h, which is what the
   vendor driver does. Naming a register we have not established would
   invite reasoning about it.
*/

#ifndef BACKEND_GENESYS_GL126_REGISTERS_H
#define BACKEND_GENESYS_GL126_REGISTERS_H

namespace genesys {
namespace gl126 {

/* Status / control */
#define REG_0x01 0x01   /* chip state; 0x22 = idle-homed, 0x00 = cold */
#define REG_0x32 0x32   /* loader state; 0x05 = magazine latched */

/* reg 0x01 bits */
#define REG_0x01_READY 0x20   /* set once the unit has homed since power-on */

/* Absolute feed length for a POSITION move (mode 0x18), big-endian
   across three registers. */
#define REG_FEEDL_HI 0x3d
#define REG_FEEDL_MID 0x3e
#define REG_FEEDL_LO 0x3f

/* Scan line count. */
#define REG_LINES_HI 0x26
#define REG_LINES_LO 0x27

/* AFE access pair: 0x5d selects, 0x5e carries the byte. Gain and offset
   codes are written through it. */
#define REG_AFE_SEL 0x5d
#define REG_AFE_DATA 0x5e

/* Extended register holding the loader sensor bit (read before the base
   table is written; it is unreliable afterwards). */
#define REG_EXT_LOADER 0x101
#define REG_EXT_LOADER_PRESENT 0x08

} // namespace gl126
} // namespace genesys

#endif // BACKEND_GENESYS_GL126_REGISTERS_H
