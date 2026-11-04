using System.Drawing;
using System.Reflection;
using System.Windows;
using RSTGameTranslation;
using Size = System.Windows.Size;

class Program
{
    static void Check(bool value, string name) { if (!value) throw new Exception(name); }
    [STAThread] static void Main()
    {
        new System.Windows.Application();
        var detector = new FrameChangeDetector();
        using var image = new Bitmap(240,80);
        using (var g = Graphics.FromImage(image)) g.Clear(System.Drawing.Color.FromArgb(100,100,100));
        Check(!detector.IsSameAsOcrFrame(image,10,20), "first frame");detector.MarkOcrFrame();
        Check(detector.IsSameAsOcrFrame(image,10,20) && detector.IsExactMatch,"strict identical frame");
        image.SetPixel(50,40,System.Drawing.Color.FromArgb(110,110,110));
        Check(detector.IsSameAsOcrFrame(image,10,20) && !detector.IsExactMatch,"tolerance cannot authorize OCR reuse");
        image.SetPixel(50,40,System.Drawing.Color.White);
        Check(!detector.IsSameAsOcrFrame(image,10,20) && !detector.IsExactMatch,"new glyph pixel");
        Check(!detector.IsSameAsOcrFrame(image,11,20),"capture origin change");
        using var resized = new Bitmap(241,80);
        Check(!detector.IsSameAsOcrFrame(resized,10,20),"capture dimensions change");
        Console.WriteLine("PASS exact frame / tolerant shimmer / changed pixels / origin / dimensions");
        Check(TextGrowth.IsGrowth("体力が減ったら回復薬を使い", "体力が減ったら回復薬を使いましょう。"),"tail growth must supersede an in-flight partial translation");
        Check(TextGrowth.IsGrowth("体力が減ったら\n設定", "体力が減ったら回復薬を\n設定"),"static menu must not hide dialogue growth");
        Check(!TextGrowth.IsGrowth("体力が減ったら", "体力が減ったら"),"same text is not growth");
        Check(!TextGrowth.EndsSentence("まだ終わらない……") && TextGrowth.EndsSentence("準備してください。"),"typewriter pause/end detection");
        Console.WriteLine("PASS typewriter tail / static labels / sentence-end protection");

        var cache = TranslationCache.Instance;
        cache.UseContext("old");cache.SetIfContext("old","line","old translation");
        cache.UseContext("new");cache.SetIfContext("old","line","late old translation");
        Check(!cache.TryGet("line",out _),"late context poisoning");
        cache.SetIfContext("new","line","new translation");
        Check(cache.TryGet("line",out var text) && text=="new translation","current context insert");
        Console.WriteLine("PASS atomic context-aware translation cache");

        ConfigManager.Instance.SetValue("nearby_translation","true");
        TextObject Make(string translation) {
            var item = new TextObject("この先は危険です。気をつけてください。",20,30,300,36);
            item.CreateUIElement(false);item.TextTranslated=translation;item.UpdateUIElement();return item;
        }
        var item=Make("前方危险，请小心前进。");
        foreach(double width in new[]{90.0,160,300,500}) foreach(double font in new[]{10.0,16,22}) {
            Size first=item.MeasureNearby(width,font);
            if(width==90) Console.WriteLine($"Measure validation width={width} font={font} actual={item.TextBlock.FontSize} line={item.TextBlock.LineHeight} desired={first} child={item.TextBlock.DesiredSize}");
            item.MeasureNearby(70,9); // A cache hit must still restore current font/wrap settings.
            Size again=item.MeasureNearby(width,font);
            Check(first==again && Math.Abs(item.TextBlock.FontSize-font)<.01,"measurement reuse or style restore");
            var fresh=Make(item.TextTranslated);
            Size freshSize=fresh.MeasureNearby(width,font);
            Check(freshSize==again,$"cached result diverged width={width} font={font} cached={again} fresh={freshSize} original={first}");
        }
        item.TextTranslated="前方十分危险，请先准备好武器和药品，然后再小心地继续向前行进。";item.UpdateUIElement();
        var changed=Make(item.TextTranslated);
        Check(item.MeasureNearby(120,18)==changed.MeasureNearby(120,18),"translation invalidation");
        item.Width=160;item.Height=24;
        changed.Width=160;changed.Height=24;
        Check(item.GetNearbyFontSize()==changed.GetNearbyFontSize(),"source geometry invalidation");
        item.TextBlock.FontFamily=new System.Windows.Media.FontFamily("Microsoft YaHei");
        changed.TextBlock.FontFamily=item.TextBlock.FontFamily;
        Check(item.MeasureNearby(120,18)==changed.MeasureNearby(120,18),"font-family invalidation");
        Console.WriteLine("PASS layout cache / restore / translation / geometry / font invalidation");
    }
}
