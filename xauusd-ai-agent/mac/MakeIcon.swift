import AppKit
let target = CommandLine.arguments[1]
try FileManager.default.createDirectory(atPath: target, withIntermediateDirectories: true)
for size in [16,32,128,256,512] {
    for scale in [1,2] {
        let pixels = size*scale
        let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
        let context = NSGraphicsContext.current!.cgContext
        context.scaleBy(x: CGFloat(pixels)/512, y: CGFloat(pixels)/512)
        let rect = NSRect(x: 22, y: 22, width: 468, height: 468)
        let background = NSBezierPath(roundedRect: rect, xRadius: 105, yRadius: 105)
        NSGradient(starting: NSColor(calibratedRed: 0.17, green: 0.20, blue: 0.21, alpha: 1), ending: NSColor(calibratedRed: 0.055, green: 0.075, blue: 0.08, alpha: 1))!.draw(in: background, angle: -60)
        NSColor(calibratedRed: 0.84, green: 0.72, blue: 0.47, alpha: 1).setStroke()
        let diamond = NSBezierPath()
        diamond.move(to: NSPoint(x: 256, y: 408));diamond.line(to: NSPoint(x: 408, y: 256));diamond.line(to: NSPoint(x: 256, y: 104));diamond.line(to: NSPoint(x: 104, y: 256));diamond.close()
        diamond.lineWidth=13;diamond.stroke()
        let inner=NSBezierPath()
        inner.move(to: NSPoint(x: 183, y: 235));inner.line(to: NSPoint(x: 232, y: 284));inner.line(to: NSPoint(x: 272, y: 244));inner.line(to: NSPoint(x: 333, y: 305))
        inner.lineWidth=16;inner.lineCapStyle = .round;inner.lineJoinStyle = .round
        NSColor(calibratedRed: 0.46, green: 0.80, blue: 0.69, alpha: 1).setStroke();inner.stroke()
        NSGraphicsContext.restoreGraphicsState()
        let name="icon_\(size)x\(size)"+(scale==2 ? "@2x" : "")+".png"
        try bitmap.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: target).appendingPathComponent(name))
    }
}
