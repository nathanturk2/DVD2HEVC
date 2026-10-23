// SPDX-License-Identifier: GPL-3.0-or-later
package org.videolan;

/** Headless tests have no libbluray JNI logger; production uses VLC's class. */
public final class Logger {
    public static Logger getLogger(String name){return new Logger();}
    public void trace(String message){}
    public void info(String message){}
    public void warning(String message){}
    public void error(String message){System.err.println(message);}
    public void unimplemented(){}
    public void unimplemented(String message){}
    public static void unimplemented(String owner,String method){}
    public static String dumpStack(){return "headless test";}
    public static String dumpStack(Throwable error){return error.toString();}
}
