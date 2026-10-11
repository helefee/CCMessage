// AgentBridge.exe: tiny launcher for the desktop build (no console window).
// Starts  runtime\pythonw.exe -m agent_bridge desktop [args]  with working dir app\, then exits.
// Built by tools\build_desktop.ps1 with the .NET Framework csc.exe (no extra SDK needed).
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Forms;

static class Launcher
{
    [DllImport("user32.dll")]
    static extern bool AllowSetForegroundWindow(int pid);

    [STAThread]
    static int Main(string[] args)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string py = Path.Combine(root, "runtime", "pythonw.exe");
        string app = Path.Combine(root, "app");
        if (!File.Exists(py) || !Directory.Exists(Path.Combine(app, "agent_bridge")))
        {
            // "Program files are incomplete, please re-extract the whole AgentBridge folder."
            MessageBox.Show("\u7a0b\u5e8f\u6587\u4ef6\u4e0d\u5168\uff1a\u627e\u4e0d\u5230 " + py +
                "\n\u8bf7\u628a\u6574\u4e2a AgentBridge \u6587\u4ef6\u5939\u91cd\u65b0\u89e3\u538b\u4e00\u904d\u3002",
                "\u4f1a\u8bdd\u6d88\u606f\u603b\u7ebf", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        var sb = new StringBuilder("-m agent_bridge desktop");
        foreach (string a in args)
            sb.Append(" \"").Append(a.Replace("\"", "\\\"")).Append('"');
        var psi = new ProcessStartInfo(py, sb.ToString());
        psi.WorkingDirectory = app;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.EnvironmentVariables["AGENT_BRIDGE_BUNDLED"] = "1";
        psi.EnvironmentVariables["PYTHONUTF8"] = "1";
        psi.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1";
        // let the python process (or an already-running instance it hands over to) bring its window to the front
        AllowSetForegroundWindow(-1);
        try { Process.Start(psi); }
        catch (Exception e)
        {
            MessageBox.Show(e.Message, "\u4f1a\u8bdd\u6d88\u606f\u603b\u7ebf", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        return 0;
    }
}
