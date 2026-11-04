using System.Windows;
using Size = System.Windows.Size;

namespace RSTGameTranslation;

public sealed record NearbyLabel(string Id, Rect Source, double FontSize, double MinFontSize,
    Func<double, double, Size> Measure);
public sealed record NearbyPlacement(string Id, Rect Bounds, double FontSize, double WrapWidth);

/// <summary>Place complete, measured labels in free space; never erase or overlap source text.</summary>
public static class NearbyOverlayLayout
{
    private const double Gap = 5;

    public static List<NearbyPlacement> Arrange(Rect viewport, IReadOnlyList<Rect> sources,
        IReadOnlyList<NearbyLabel> labels)
    {
        var result = new List<NearbyPlacement>();
        if (viewport.Width < 16 || viewport.Height < 16) return result;
        viewport.Inflate(-6, -6);
        var free = new List<Rect> { viewport };
        foreach (var source in sources) free = Subtract(free, source);
        // Reading order makes the same frame deterministic; paragraphs stay together.
        foreach (var label in labels.OrderBy(l => l.Source.Top).ThenBy(l => l.Source.Left))
        {
            bool denseColumn = sources.Any(r => r != label.Source &&
                Math.Min(r.Right, label.Source.Right) - Math.Max(r.Left, label.Source.Left) > Math.Min(r.Width, label.Source.Width) * .5 &&
                Math.Max(r.Top - label.Source.Bottom, label.Source.Top - r.Bottom) >= 0 &&
                Math.Max(r.Top - label.Source.Bottom, label.Source.Top - r.Bottom) < label.FontSize * 2 + 10);
            NearbyPlacement? best = null;
            foreach (double font in new[] { label.FontSize, label.MinFontSize }.Distinct())
            {
                double bestScore = double.PositiveInfinity;
                var measured = new Dictionary<double, Size>();
                foreach (var space in free.OrderBy(r => Distance(r, label.Source)).Take(32))
                {
                    double preferred = Math.Clamp(label.Source.Width, 140, 340);
                    foreach (double width in new[] { Math.Min(space.Width, preferred), Math.Min(space.Width, 420) }.Distinct())
                    {
                        if (width < 40) continue;
                        if (!measured.TryGetValue(width, out var size))
                            measured[width] = size = label.Measure(width, font);
                        if (size.Width > space.Width + .01 || size.Height > space.Height + .01 ||
                            size.Width <= 0 || size.Height <= 0) continue;
                        double x = Math.Clamp(label.Source.Left, space.Left, space.Right - size.Width);
                        double[] ys = { label.Source.Bottom + Gap, label.Source.Top - size.Height - Gap,
                            label.Source.Top };
                        foreach (double y in ys)
                        {
                            var box = new Rect(x, Math.Clamp(y, space.Top, space.Bottom - size.Height), size.Width, size.Height);
                            double sidePenalty = denseColumn
                                ? box.Left >= label.Source.Right ? 0 : box.Right <= label.Source.Left ? 24 : 26
                                : box.Top >= label.Source.Bottom ? 0 : box.Bottom <= label.Source.Top ? 8 :
                                  box.Left >= label.Source.Right ? 12 : 40;
                            double score = Distance(box, label.Source) + sidePenalty +
                                Math.Abs(box.Left - label.Source.Left) * .05 + size.Height * .8 +
                                (box.Left >= label.Source.Right || box.Right <= label.Source.Left
                                    ? Math.Abs(box.Top - label.Source.Top) * .4 : 0);
                            if (score < bestScore)
                            {
                                bestScore = score;
                                best = new NearbyPlacement(label.Id, box, font, width);
                            }
                        }
                    }
                }
                if (best != null) break; // Shrink only when no location fits at the preferred font.
            }
            if (best == null) continue; // No physical room: preserve the original rather than cover it.
            result.Add(best);
            free = Subtract(free, best.Bounds);
        }
        return result;
    }

    private static double Distance(Rect a, Rect b)
    {
        double dx = Math.Max(0, Math.Max(a.Left - b.Right, b.Left - a.Right));
        double dy = Math.Max(0, Math.Max(a.Top - b.Bottom, b.Top - a.Bottom));
        return Math.Sqrt(dx * dx + dy * dy);
    }

    // Maximal free rectangles may overlap; subtracting every placed label keeps all candidates safe.
    private static List<Rect> Subtract(List<Rect> spaces, Rect obstacle)
    {
        obstacle.Inflate(Gap, Gap);
        var next = new List<Rect>();
        foreach (var space in spaces)
        {
            var hit = Rect.Intersect(space, obstacle);
            if (hit.IsEmpty || hit.Width <= 0 || hit.Height <= 0) { next.Add(space); continue; }
            if (hit.Left > space.Left) next.Add(new Rect(space.Left, space.Top, hit.Left - space.Left, space.Height));
            if (hit.Right < space.Right) next.Add(new Rect(hit.Right, space.Top, space.Right - hit.Right, space.Height));
            if (hit.Top > space.Top) next.Add(new Rect(space.Left, space.Top, space.Width, hit.Top - space.Top));
            if (hit.Bottom < space.Bottom) next.Add(new Rect(space.Left, hit.Bottom, space.Width, space.Bottom - hit.Bottom));
        }
        var pruned = new List<Rect>();
        foreach (var space in next.Where(r => r.Width >= 40 && r.Height >= 12).Distinct().OrderByDescending(r => r.Width * r.Height))
            if (!pruned.Any(r => r.Contains(space))) pruned.Add(space);
        return pruned.Take(512).ToList();
    }
}
