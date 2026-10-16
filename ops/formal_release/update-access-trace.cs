// External diagnostic only. Never starts, modifies or injects into Worker.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using Microsoft.Diagnostics.Tracing;
using Microsoft.Diagnostics.Tracing.Parsers;
using Microsoft.Diagnostics.Tracing.Session;

class UpdateAccessTrace {
    const string Prefix = "CheJinUpdateAccess-";
    const long OutputLimit = 64L * 1024 * 1024;
    const int PendingLimit = 32768;
    static JavaScriptSerializer json = new JavaScriptSerializer();
    static string sessionName, output, root, deviceRoot;
    static TraceEventSession session;
    static Process guard;
    static StreamWriter writer;
    static Dictionary<ulong, Dictionary<string,object>> pending = new Dictionary<ulong, Dictionary<string,object>>();
    static HashSet<ulong> ambiguousIrps = new HashSet<ulong>();
    static int stopped, collisions;
    static int? lost;
    static long bytes;
    static long sequence;
    static string reason = "not_started";
    static object bufferConfiguration;
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode)]
    static extern uint QueryDosDevice(string device, StringBuilder target, int length);
    [StructLayout(LayoutKind.Sequential)] struct Wnode {
        public uint size, provider; public ulong context; public long timestamp;
        public Guid guid; public uint clock, flags;
    }
    [StructLayout(LayoutKind.Sequential)] struct Properties {
        public Wnode wnode;
        public uint sizeKB, minimum, maximum, maxFileMB, mode, flush, enable, age;
        public uint allocated, free, lost, written, logLost, realTimeLost;
        public IntPtr thread; public uint fileOffset, nameOffset;
    }
    [DllImport("advapi32.dll",CharSet=CharSet.Unicode)]
    static extern uint ControlTraceW(ulong handle,string name,IntPtr properties,uint control);
    static Properties QueryBuffers() {
        IntPtr memory=Marshal.AllocHGlobal(4096);
        try {
            var props=new Properties(); props.wnode.size=4096; props.nameOffset=(uint)Marshal.SizeOf(typeof(Properties));
            Marshal.StructureToPtr(props,memory,false);
            uint error=ControlTraceW(0,sessionName,memory,0);
            if (error!=0) throw new System.ComponentModel.Win32Exception((int)error);
            return (Properties)Marshal.PtrToStructure(memory,typeof(Properties));
        } finally { Marshal.FreeHGlobal(memory); }
    }
    static void Save(string name, object data) {
        File.WriteAllText(Path.Combine(output, name), json.Serialize(data), new UTF8Encoding(false));
    }
    static void Stop(string why) {
        if (Interlocked.Exchange(ref stopped, 1) != 0) return;
        reason = why;
        ThreadPool.QueueUserWorkItem(delegate {
            try { lost = session.EventsLost; } catch { lost=null; }
            // Stopping never depends on whether querying statistics succeeded.
            try { session.Stop(); } catch { /* Independent watchdog also owns this exact session. */ }
        });
    }
    static string Relative(string value) {
        if (String.IsNullOrEmpty(value)) return null;
        if (value.StartsWith(@"\\?\")) value = value.Substring(4);
        foreach (string prefix in new string[] {root, deviceRoot}) {
            if (prefix == null) continue;
            if (value.Equals(prefix, StringComparison.OrdinalIgnoreCase)) return ".";
            if (value.StartsWith(prefix + @"\", StringComparison.OrdinalIgnoreCase)) return value.Substring(prefix.Length+1);
        }
        return null;
    }
    static void Write(Dictionary<string,object> data) {
        string line = json.Serialize(data);
        long size = Encoding.UTF8.GetByteCount(line) + Encoding.UTF8.GetByteCount(writer.NewLine);
        if (bytes + size > OutputLimit) { Stop("output_limit_evidence_incomplete"); return; }
        writer.WriteLine(line); bytes += size;
    }
    static void Begin(TraceEvent e, string path, string operation, ulong irp, object detail) {
        string relative = Relative(path);
        if (relative == null) return;
        string processName = "unknown_or_exited";
        try { using (var process = Process.GetProcessById(e.ProcessID)) processName = process.ProcessName; } catch { }
        var row = new Dictionary<string,object> {
            {"sequence",++sequence},
            {"utc", e.TimeStamp.ToUniversalTime().ToString("o")}, {"operation",operation},
            {"path",relative}, {"pid",e.ProcessID}, {"tid",e.ThreadID}, {"process",processName}, {"detail",detail}
        };
        Write(row);
        if (irp != 0) {
            if (pending.Count + ambiguousIrps.Count >= PendingLimit) { Stop("correlation_limit_evidence_incomplete"); return; }
            if (ambiguousIrps.Contains(irp)) return;
            if (pending.ContainsKey(irp)) {
                pending.Remove(irp); ambiguousIrps.Add(irp); collisions++;
                Write(new Dictionary<string,object> {{"operation","CorrelationConflict"},{"path",relative},{"sequence",row["sequence"]}});
                return;
            }
            pending[irp] = row;
        }
    }
    static bool Exists(string name) { return TraceEventSession.GetActiveSessionNames().Contains(name); }
    static void StopOwn(string name) {
        Guid id;
        if (!name.StartsWith(Prefix) || !Guid.TryParse(name.Substring(Prefix.Length), out id)) throw new ArgumentException("Invalid owned session");
        if (Exists(name)) using (var owned = new TraceEventSession(name, TraceEventSessionOptions.Attach)) owned.Stop();
    }
    static int Watch(string[] args) {
        int pid = Int32.Parse(args[1]); long start = Int64.Parse(args[2]);
        string name = args[3]; output = args[4]; DateTime end = DateTime.UtcNow.AddMinutes(32);
        while (DateTime.UtcNow < end) {
            bool live = false;
            try { using (var p = Process.GetProcessById(pid)) live = p.StartTime.ToUniversalTime().Ticks == start && !p.HasExited; } catch { }
            if (!live) break;
            Thread.Sleep(500);
        }
        try { StopOwn(name); Save("watchdog.json", new {owned_session_stopped = !Exists(name)}); return 0; }
        catch (Exception e) { Save("watchdog.json", new {cleanup_error_type=e.GetType().Name}); return 1; }
    }
    static void Plain(string path) {
        for (var dir = new DirectoryInfo(path); dir != null; dir = dir.Parent)
            if ((dir.Attributes & FileAttributes.ReparsePoint) != 0) throw new IOException("Reparse path refused");
    }
    static string NewDownload(HashSet<string> baseline) {
        foreach (string request in Directory.GetDirectories(Path.Combine(root,"requests"),"update-*")) {
            if (baseline.Contains(request)) continue;
            Plain(request);
            if (File.Exists(Path.Combine(request,"download","client-update.zip.partial")) ||
                File.Exists(Path.Combine(request,"download","client-update.zip"))) return request;
        }
        return null;
    }
    static int Collect(string[] args) {
        root = Path.GetFullPath(args[1]).TrimEnd('\\'); output = Path.GetFullPath(args[2]);
        if (Path.GetFileName(root) != "CheJinWorkerUpdate") throw new ArgumentException("Unexpected cache root");
        if (!Directory.Exists(output)) throw new IOException("Output directory must already exist");
        Plain(root); Plain(output);
        if (output.StartsWith(root + @"\", StringComparison.OrdinalIgnoreCase)) throw new ArgumentException("Output must be outside observed cache");
        if (Environment.OSVersion.Version < new Version(6,2)) throw new NotSupportedException("Independent system logger requires Windows 8 or newer");
        if (TraceEventSession.IsElevated() != true) throw new UnauthorizedAccessException("Administrator diagnostic permission required");
        int seconds = Int32.Parse(args[3]), armSeconds = Int32.Parse(args[4]);
        if (seconds < 1 || seconds > 1200 || armSeconds < 1 || armSeconds > 600) throw new ArgumentException("Diagnostic duration outside limits");
        var mapping = new StringBuilder(1024);
        if (root.Length > 2 && root[1] == ':' && QueryDosDevice(root.Substring(0,2), mapping, mapping.Capacity) != 0)
            deviceRoot = mapping.ToString() + root.Substring(2);
        sessionName = Prefix + Guid.NewGuid().ToString();
        if (Exists(sessionName)) throw new InvalidOperationException("Session collision");
        var self = Process.GetCurrentProcess();
        guard = Process.Start(new ProcessStartInfo(self.MainModule.FileName,
            "--watch " + self.Id + " " + self.StartTime.ToUniversalTime().Ticks + " " + sessionName + " \"" + output + "\"") {
                UseShellExecute=false, CreateNoWindow=true
            });
        using (guard) {
            try {
                var baseline = new HashSet<string>(Directory.GetDirectories(Path.Combine(root,"requests"),"update-*"),StringComparer.OrdinalIgnoreCase);
                DateTime waitEnd = DateTime.UtcNow.AddSeconds(armSeconds);
                string activeRequest = null;
                using (session = new TraceEventSession(sessionName,TraceEventSessionOptions.Create | TraceEventSessionOptions.NoPerProcessorBuffering)) {
                    session.BufferSizeMB = 16;
                    session.StopOnDispose = true;
                    session.EnableKernelProvider(KernelTraceEventParser.Keywords.FileIO | KernelTraceEventParser.Keywords.FileIOInit);
                    var buffers=QueryBuffers();
                    if ((long)buffers.maximum*buffers.sizeKB > 32L*1024) throw new IOException("OS-adjusted ETW buffer pool exceeds diagnostic threshold");
                    bufferConfiguration=new {buffer_kb=buffers.sizeKB,minimum=buffers.minimum,maximum=buffers.maximum,allocated=buffers.allocated,
                        maximum_pool_mb=buffers.maximum*buffers.sizeKB/1024.0,requested_minimum_mb=16,not_total_process_memory=true};
                    reason = "capturing";
                    using (writer = new StreamWriter(Path.Combine(output,"cache-events.jsonl"),false,new UTF8Encoding(false))) {
                        var source = session.Source;
                        source.Kernel.FileIOCreate += e => Begin(e,e.FileName,"Create",e.IrpPtr,new {share_access=(int)e.ShareAccess,create_options=(int)e.CreateOptions});
                        source.Kernel.FileIORename += e => Begin(e,e.FileName,"Rename",e.IrpPtr,new {info_class=e.InfoClass});
                        source.Kernel.FileIOSetInfo += e => Begin(e,e.FileName,"SetInfo",e.IrpPtr,new {info_class=e.InfoClass});
                        source.Kernel.FileIODelete += e => Begin(e,e.FileName,"Delete",e.IrpPtr,new {info_class=e.InfoClass});
                        source.Kernel.FileIOCleanup += e => Begin(e,e.FileName,"Cleanup",0,null);
                        source.Kernel.FileIOClose += e => Begin(e,e.FileName,"Close",0,null);
                        source.Kernel.FileIOOperationEnd += e => {
                            Dictionary<string,object> start;
                            if (!pending.TryGetValue(e.IrpPtr,out start)) return;
                            pending.Remove(e.IrpPtr);
                            Write(new Dictionary<string,object> {{"utc",e.TimeStamp.ToUniversalTime().ToString("o")},{"operation","OperationEnd"},
                                {"sequence",start["sequence"]},
                                {"for_operation",start["operation"]},{"path",start["path"]},{"pid",start["pid"]},{"tid",start["tid"]},
                                {"process",start["process"]},{"ntstatus",unchecked((uint)e.NtStatus).ToString("X8")}});
                        };
                        Console.CancelKeyPress += (s,e) => { e.Cancel=true; Stop("operator_ctrl_c"); };
                        DateTime deadline = waitEnd;
                        bool stagingSeen = false;
                        DateTime? stagingEnded = null;
                        object monitorLock = new object();
                        using (var timer = new Timer(delegate {
                          lock (monitorLock) {
                            try {
                                if (activeRequest == null) {
                                    activeRequest=NewDownload(baseline);
                                    if (activeRequest!=null) { deadline=DateTime.UtcNow.AddSeconds(seconds); Save("capturing.json",new {capturing=true,session=sessionName}); }
                                }
                                if (DateTime.UtcNow > deadline) Stop(activeRequest==null ? "no_request_time_limit_evidence_incomplete" : "capture_time_limit_evidence_incomplete");
                                else if (guard.HasExited) Stop("watchdog_lost_evidence_incomplete");
                                else if (File.Exists(Path.Combine(output,"stop.request"))) Stop("operator_stop");
                                else {
                                    using (var proc=Process.GetCurrentProcess()) if (proc.PrivateMemorySize64 > 512L*1024*1024) Stop("parser_memory_limit_evidence_incomplete");
                                    var nowBuffers=QueryBuffers();
                                    if ((long)nowBuffers.allocated*nowBuffers.sizeKB > 32L*1024) Stop("os_buffer_pool_limit_evidence_incomplete");
                                    if (activeRequest==null) return;
                                    if (Directory.Exists(Path.Combine(activeRequest,"staging.extracting"))) { stagingSeen=true; stagingEnded=null; }
                                    else if (stagingSeen) {
                                        if (!stagingEnded.HasValue) stagingEnded=DateTime.UtcNow;
                                        if ((DateTime.UtcNow-stagingEnded.Value).TotalSeconds > 2)
                                            Stop(Directory.Exists(Path.Combine(activeRequest,"staging")) ? "staging_promotion_observed_not_upgrade_acceptance" : "staging_departure_or_cleanup_observed_not_upgrade_acceptance");
                                    }
                                }
                            } catch { Stop("monitor_error_evidence_incomplete"); }
                          }
                        },null,500,500)) {
                            // Provider is already active and all callbacks are installed BEFORE
                            // ARMED; even immediate events are retained in real-time ETW buffers.
                            Save("armed.json",new {armed=true,cache_scope="CheJinWorkerUpdate",output_filter="in-memory full-system metadata; only cache-relative records saved",session=sessionName,buffers=bufferConfiguration});
                            Console.WriteLine("ARMED. Keep Worker stopped from accepting work. Do NOT elevate Worker.");
                            Console.WriteLine("Wait for the operator to enable the controlled update, then click Check update ONCE.");
                            Console.WriteLine("CAPTURING update-cache metadata only. Close this window or press Ctrl+C to stop.");
                            source.Process();
                        }
                        writer.Flush();
                    }
                }
                return 0;
            } finally {
                StopOwn(sessionName);
                Save("summary.json",new {reason=reason,events_lost=lost,output_bytes=bytes,unmatched_cache_operations=pending.Count,
                    correlation_conflicts=collisions,
                    evidence_may_be_incomplete=lost!=0 || collisions!=0 || pending.Count!=0 || reason.EndsWith("incomplete"),
                    own_session_remaining=Exists(sessionName),not_a_repair=true,worker_not_started_or_modified=true,
                    buffers=bufferConfiguration,limits=new {etw_pool_threshold_mb=32,output_mb=64,pending_operations=PendingLimit,parser_private_mb=512}});
            }
        }
    }
    public static int Main(string[] args) {
        try {
            if (args.Length==5 && args[0]=="--watch") return Watch(args);
            if (args.Length==5 && args[0]=="--collect") return Collect(args);
            Console.WriteLine("Use the supplied diagnostic launcher."); return 2;
        } catch(Exception e) {
            // Exception messages/stack traces can contain unrelated absolute paths.
            Console.WriteLine("Diagnostic stopped: " + e.GetType().Name + "; no automatic retry.");
            try { if (output != null && Directory.Exists(output)) Save("error.json",new {type=e.GetType().Name,hresult=e.HResult}); } catch { }
            return 1;
        }
    }
}
