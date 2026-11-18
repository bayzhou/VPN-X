using System;
using System.Diagnostics;
using System.IO;
using System.Security.AccessControl;
using System.Text.RegularExpressions;
using System.Windows.Forms;

static class Launcher
{
    static bool Trusted(string sid)
    {
        return sid == "S-1-5-18" || sid == "S-1-5-32-544" ||
               sid == "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464";
    }

    static bool SafeDirectory(string path)
    {
        var dir = new DirectoryInfo(Path.GetFullPath(path));
        if (new DriveInfo(dir.Root.FullName).DriveType != DriveType.Fixed || dir.Parent == null)
            return false;
        if (File.Exists(dir.FullName) || (dir.Exists && dir.GetFileSystemInfos().Length != 0))
            return false;
        bool leaf = true;
        for (var current = dir; current != null; current = current.Parent)
        {
            if (!current.Exists)
            {
                if (!leaf) return false;
                leaf = false;
                continue;
            }
            if ((current.Attributes & FileAttributes.ReparsePoint) != 0) return false;
            var acl = current.GetAccessControl();
            if (!Trusted(acl.GetOwner(typeof(System.Security.Principal.SecurityIdentifier)).Value)) return false;
            foreach (FileSystemAccessRule rule in acl.GetAccessRules(true, true, typeof(System.Security.Principal.SecurityIdentifier)))
            {
                if (rule.AccessControlType != AccessControlType.Allow || Trusted(rule.IdentityReference.Value)) continue;
                bool applies = (rule.PropagationFlags & PropagationFlags.InheritOnly) == 0;
                var dangerous = FileSystemRights.Delete | FileSystemRights.DeleteSubdirectoriesAndFiles |
                                FileSystemRights.TakeOwnership | FileSystemRights.ChangePermissions;
                var writes = FileSystemRights.Write | dangerous;
                // The volume root itself cannot be deleted; permissions to delete its children still matter.
                if (current.Parent == null) dangerous &= ~FileSystemRights.Delete;
                if (applies && (rule.FileSystemRights & (leaf ? writes : dangerous)) != 0) return false;
                // For a new leaf require safe inheritance as well as a safe parent. Windows resolves
                // CREATOR OWNER to the elevated installer that actually creates the directory.
                if (!dir.Exists && current.FullName == dir.Parent.FullName &&
                    (rule.InheritanceFlags & (InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit)) != 0 &&
                    rule.IdentityReference.Value != "S-1-3-0" && (rule.FileSystemRights & writes) != 0)
                    return false;
            }
            leaf = false;
        }
        return true;
    }

    [STAThread]
    static int Main(string[] args)
    {
        try
        {
            if (args.Length == 2 && args[0] == "--check-new-directory")
                return SafeDirectory(args[1]) ? 0 : 1;
            string root = AppDomain.CurrentDomain.BaseDirectory;
            string version = File.ReadAllText(Path.Combine(root, "current-version.txt")).Trim();
            if (!Regex.IsMatch(version, @"\A(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})\z"))
                throw new InvalidDataException();
            string folder = Path.Combine(root, "versions", version);
            Process.Start(new ProcessStartInfo(Path.Combine(folder, "VPN-X.exe")) { WorkingDirectory = folder });
            return 0;
        }
        catch
        {
            if (args.Length == 0) MessageBox.Show("无法启动 VPN-X，请检查安装目录。", "VPN-X",
                                                  MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
