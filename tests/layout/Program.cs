using System.Windows;
using System.Diagnostics;
using System.Text.Json;
using RSTGameTranslation;
class Program {
 [STAThread]static void Main(string[] args){
  new Application();ConfigManager.Instance.SetValue("nearby_translation","true");
  var report=new List<object>();
  foreach(int count in new[]{5,16,40}){
   var items=Enumerable.Range(0,count).Select(i=>{var t=new TextObject("新しい道具を手に入れたら、装備を確認してください。",60+(i%4)*460,70+(i/4)*92,360,36);t.CreateUIElement(false);t.TextTranslated="获得新的道具后，请检查装备是否准备完毕。";t.UpdateUIElement();return t;}).ToList();
   var sources=items.Select(t=>new Rect(t.X,t.Y,t.Width,t.Height)).ToList();
   var times=new List<double>();int placedCount=0;
   for(int run=0;run<14;run++){
    var watch=Stopwatch.StartNew();
    var labels=items.Select(t=>new NearbyLabel(t.ID,new Rect(t.X,t.Y,t.Width,t.Height),t.GetNearbyFontSize(),10,t.MeasureNearby)).ToList();
    var placed=NearbyOverlayLayout.Arrange(new Rect(0,0,1920,1080),sources,labels);
    foreach(var p in placed)items.Single(t=>t.ID==p.Id).PlaceNearby(p);
    watch.Stop();placedCount=placed.Count;if(run>=4)times.Add(watch.Elapsed.TotalMilliseconds);
   }
   times.Sort();report.Add(new{blocks=count,placed=placedCount,median_ms=(times[4]+times[5])/2,min_ms=times[0],max_ms=times[^1],note="warm repeated layout including source-font estimation, excludes DWM presentation"});
   Console.WriteLine($"{count} labels: median={(times[4]+times[5])/2:F2}ms, placed={placedCount}");
  }
  System.IO.File.WriteAllText(args[0],JsonSerializer.Serialize(report,new JsonSerializerOptions{WriteIndented=true}));
 }
}
