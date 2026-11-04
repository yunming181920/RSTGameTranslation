using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Text.Json;
using System.Threading;

namespace RSTGameTranslation;

// A separate process keeps DirectML's native runtime separate from TTS/STT ONNX.
internal sealed class RapidOCRManager
{
    public const string Method = "RapidOCR (DirectML)";
    public static RapidOCRManager Instance { get; } = new();
    private readonly SemaphoreSlim gate = new(1, 1);
    // Requests are serialized by gate. Reuse the BMP buffer rather than allocate
    // another ~33 MB managed array for every 4K frame.
    private readonly MemoryStream imageBuffer = new();
    private Process? worker;
    private long requestId;

    private RapidOCRManager()
    {
        System.Windows.Application.Current.Exit += (_, _) => Stop();
    }

    private void Stop()
    {
        var process = worker;
        worker = null;
        if (process == null) return;
        try { if (!process.HasExited) process.Kill(entireProcessTree: true); }
        catch (InvalidOperationException) { }
        finally { process.Dispose(); }
    }

    private async Task StartAsync()
    {
        if (worker != null && !worker.HasExited) return;
        Stop();
        string baseDir = AppContext.BaseDirectory;
        string python = Environment.GetEnvironmentVariable("RST_OCR_PYTHON")
            ?? Path.Combine(baseDir, "gpu_ocr", "python", "python.exe");
        string script = Environment.GetEnvironmentVariable("RST_OCR_SCRIPT")
            ?? Path.Combine(baseDir, "gpu_ocr", "backend.py");
        if (!File.Exists(python) || !File.Exists(script))
            throw new FileNotFoundException("RapidOCR files are missing. Run gpu_ocr/verify.ps1 to check the installation.");
        var start = new ProcessStartInfo(python)
        {
            WorkingDirectory = Path.GetDirectoryName(python)!,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        start.ArgumentList.Add("-u");
        start.ArgumentList.Add(script);
        worker = Process.Start(start) ?? throw new IOException("Cannot start RapidOCR");
        worker.ErrorDataReceived += (_, e) => { if (e.Data != null) Console.WriteLine("RapidOCR: " + e.Data); };
        worker.BeginErrorReadLine();
        string? ready = await worker.StandardOutput.ReadLineAsync().WaitAsync(TimeSpan.FromSeconds(90));
        using var message = JsonDocument.Parse(ready ?? throw new IOException("RapidOCR exited during startup"));
        if (!message.RootElement.GetProperty("ready").GetBoolean())
            throw new IOException("RapidOCR startup failed");
        Console.WriteLine("RapidOCR ready: " + ready);
    }

    public async Task WarmUpAsync()
    {
        await gate.WaitAsync();
        try { await StartAsync(); }
        catch { Stop(); throw; }
        finally { gate.Release(); }
    }

    public async Task ProcessAsync(Bitmap bitmap)
    {
        await gate.WaitAsync();
        try
        {
            var total = Stopwatch.StartNew();
            await StartAsync();
            double startupMs = total.Elapsed.TotalMilliseconds;
            var stream = imageBuffer;
            stream.Position = 0;
            stream.SetLength(0);
            bitmap.Save(stream, ImageFormat.Bmp);
            double encodeMs = total.Elapsed.TotalMilliseconds - startupMs;
            long id = ++requestId;
            await worker!.StandardInput.WriteLineAsync(JsonSerializer.Serialize(new
            {
                id,
                image_bytes = stream.Length
            }));
            await worker.StandardInput.FlushAsync();
            stream.Position = 0;
            await stream.CopyToAsync(worker.StandardInput.BaseStream);
            await worker.StandardInput.BaseStream.FlushAsync();
            string? reply = await worker.StandardOutput.ReadLineAsync().WaitAsync(TimeSpan.FromSeconds(30));
            using var response = JsonDocument.Parse(reply ?? throw new IOException("RapidOCR worker exited"));
            var root = response.RootElement;
            if (root.GetProperty("id").GetInt64() != id) throw new IOException("RapidOCR response ID mismatch");
            if (root.TryGetProperty("error", out var error)) throw new IOException(error.GetString());
            var results = root.GetProperty("lines").EnumerateArray().Select(line => new
            {
                text = line.GetProperty("text").GetString(),
                confidence = line.GetProperty("confidence").GetDouble(),
                rect = line.GetProperty("box").Deserialize<double[][]>()
            }).ToArray();
            string json = JsonSerializer.Serialize(new { status = "success", results,
                processing_time_seconds = root.GetProperty("elapsed_ms").GetDouble() / 1000 });
            Console.WriteLine($"[RapidOCR timing] startup={startupMs:F1}ms encode={encodeMs:F1}ms " +
                $"worker={root.GetProperty("elapsed_ms").GetDouble():F1}ms " +
                $"before_ui={total.Elapsed.TotalMilliseconds:F1}ms bytes={stream.Length}");
            System.Windows.Application.Current.Dispatcher.Invoke(() => Logic.Instance.ProcessReceivedTextJsonData(json));
        }
        catch
        {
            // A timed-out reply must never be consumed as the next frame's reply.
            Stop();
            throw;
        }
        finally { gate.Release(); }
    }
}
