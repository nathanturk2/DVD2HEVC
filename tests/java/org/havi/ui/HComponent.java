// SPDX-License-Identifier: GPL-3.0-or-later
package org.havi.ui;

/** Test-only AWT host: libbluray's HComponent constructor needs a native Xlet context.
 * No input/navigation behavior is mocked. Production compiles against the real API.
 */
public abstract class HComponent extends java.awt.Component {}
