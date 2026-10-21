#import <Cocoa/Cocoa.h>
#include <arpa/inet.h>
#include <sys/socket.h>
#include <unistd.h>

@interface DiskLauncher : NSObject <NSApplicationDelegate, NSWindowDelegate>
@property(nonatomic, strong) NSWindow *window;
@property(nonatomic, strong) NSTextField *status;
@property(nonatomic, strong) NSProgressIndicator *spinner;
@property(nonatomic, strong) NSButton *openButton;
@property(nonatomic, strong) NSButton *retryButton;
@property(nonatomic, copy) NSArray<NSString *> *options;
@property(nonatomic) NSUInteger port;
@property(nonatomic) BOOL noBrowser;
@property(nonatomic) BOOL starting;
@property(nonatomic) BOOL stopping;
@property(nonatomic) BOOL ready;
@property(nonatomic) BOOL terminateRequested;
@end

@implementation DiskLauncher
- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    self.port = 8766;
    NSMutableArray *options = [NSMutableArray array];
    NSArray *arguments = NSProcessInfo.processInfo.arguments;
    for (NSUInteger index = 1; index < arguments.count; index++) {
        NSString *argument = arguments[index];
        if ([argument isEqualToString:@"--no-browser"]) self.noBrowser = YES;
        else if (([argument isEqualToString:@"--port"] || [argument isEqualToString:@"--state-dir"]) && index + 1 < arguments.count) {
            NSString *value = arguments[++index];
            if ([argument isEqualToString:@"--port"]) {
                NSScanner *scanner = [NSScanner scannerWithString:value];
                NSInteger number;
                if (![scanner scanInteger:&number] || !scanner.isAtEnd || number < 1024 || number > 65535) {
                    [self fail:@"Приложение на этом Mac: порт должен быть от 1024 до 65535."];
                    return;
                }
                self.port = (NSUInteger)number;
            }
            [options addObjectsFromArray:@[argument, value]];
        }
    }
    self.options = options;
    [self makeWindow];
    [self start:nil];
}

- (void)makeWindow {
    self.window = [[NSWindow alloc] initWithContentRect:NSMakeRect(0, 0, 500, 280)
        styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable
        backing:NSBackingStoreBuffered defer:NO];
    self.window.title = @"YaDiskDownloader";
    self.window.delegate = self;
    self.window.releasedWhenClosed = NO;
    NSVisualEffectView *surface = [[NSVisualEffectView alloc] init];
    surface.material = NSVisualEffectMaterialWindowBackground;
    surface.blendingMode = NSVisualEffectBlendingModeBehindWindow;
    surface.state = NSVisualEffectStateActive;
    self.window.contentView = surface;

    NSImageView *icon = [NSImageView imageViewWithImage:NSApp.applicationIconImage];
    icon.imageScaling = NSImageScaleProportionallyUpOrDown;
    [icon.widthAnchor constraintEqualToConstant:64].active = YES;
    [icon.heightAnchor constraintEqualToConstant:64].active = YES;
    NSTextField *title = [NSTextField labelWithString:@"Ваш Диск — на этом Mac"];
    title.font = [NSFont systemFontOfSize:21 weight:NSFontWeightSemibold];
    self.status = [NSTextField wrappingLabelWithString:@"Запускаем приложение…"];
    self.status.textColor = NSColor.secondaryLabelColor;
    self.status.font = [NSFont systemFontOfSize:13];
    self.status.alignment = NSTextAlignmentCenter;
    [self.status.widthAnchor constraintEqualToConstant:430].active = YES;
    self.spinner = [[NSProgressIndicator alloc] init];
    self.spinner.style = NSProgressIndicatorStyleSpinning;
    self.spinner.controlSize = NSControlSizeSmall;
    [self.spinner startAnimation:nil];
    self.openButton = [NSButton buttonWithTitle:@"Открыть интерфейс" target:self action:@selector(openBrowser:)];
    self.openButton.bezelStyle = NSBezelStyleRounded;
    self.openButton.keyEquivalent = @"\r";
    self.openButton.enabled = NO;
    self.retryButton = [NSButton buttonWithTitle:@"Повторить запуск" target:self action:@selector(start:)];
    self.retryButton.hidden = YES;
    NSButton *quit = [NSButton buttonWithTitle:@"Выход" target:NSApp action:@selector(terminate:)];
    NSStackView *buttons = [NSStackView stackViewWithViews:@[self.retryButton, self.openButton, quit]];
    buttons.orientation = NSUserInterfaceLayoutOrientationHorizontal;
    buttons.spacing = 8;
    NSStackView *stack = [NSStackView stackViewWithViews:@[icon, title, self.status, self.spinner, buttons]];
    stack.orientation = NSUserInterfaceLayoutOrientationVertical;
    stack.alignment = NSLayoutAttributeCenterX;
    stack.spacing = 12;
    stack.translatesAutoresizingMaskIntoConstraints = NO;
    [surface addSubview:stack];
    [NSLayoutConstraint activateConstraints:@[
        [stack.centerXAnchor constraintEqualToAnchor:surface.centerXAnchor],
        [stack.centerYAnchor constraintEqualToAnchor:surface.centerYAnchor],
        [stack.leadingAnchor constraintGreaterThanOrEqualToAnchor:surface.leadingAnchor constant:20],
        [stack.trailingAnchor constraintLessThanOrEqualToAnchor:surface.trailingAnchor constant:-20]
    ]];
    NSMenu *menu = [[NSMenu alloc] init];
    NSMenuItem *applicationItem = [[NSMenuItem alloc] init];
    NSMenu *applicationMenu = [[NSMenu alloc] initWithTitle:@"YaDiskDownloader"];
    [applicationMenu addItemWithTitle:@"Открыть интерфейс" action:@selector(openBrowser:) keyEquivalent:@"o"].target = self;
    [applicationMenu addItem:NSMenuItem.separatorItem];
    [applicationMenu addItemWithTitle:@"Выйти из YaDiskDownloader" action:@selector(terminate:) keyEquivalent:@"q"];
    applicationItem.submenu = applicationMenu;
    [menu addItem:applicationItem];
    NSApp.mainMenu = menu;
    [self.window center];
    [self.window makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];
}

- (void)runCommand:(NSString *)command completion:(void (^)(int, NSString *))completion {
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        NSTask *task = [[NSTask alloc] init];
        task.executableURL = [NSBundle.mainBundle.executableURL.URLByDeletingLastPathComponent URLByAppendingPathComponent:@"yadisk"];
        NSMutableArray *arguments = [self.options mutableCopy];
        [arguments addObjectsFromArray:@[@"--json", command]];
        task.arguments = arguments;
        task.standardInput = [NSFileHandle fileHandleWithNullDevice];
        NSPipe *pipe = [NSPipe pipe];
        task.standardOutput = pipe;
        task.standardError = pipe;
        NSError *error = nil;
        if (![task launchAndReturnError:&error]) {
            dispatch_async(dispatch_get_main_queue(), ^{ completion(1, error.localizedDescription); });
            return;
        }
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 60 * NSEC_PER_SEC), dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
            if (task.running) [task terminate];
        });
        NSData *data = [pipe.fileHandleForReading readDataToEndOfFile];
        [task waitUntilExit];
        NSString *text = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding] ?: @"Не удалось прочитать ответ приложения.";
        if (text.length > 1600) text = [text substringToIndex:1600];
        int status = task.terminationStatus;
        dispatch_async(dispatch_get_main_queue(), ^{ completion(status, text); });
    });
}

- (void)start:(id)sender {
    if (self.starting || self.stopping) return;
    self.starting = YES;
    self.retryButton.hidden = YES;
    self.status.stringValue = @"Запускаем приложение и открываем историю…";
    self.spinner.hidden = NO;
    [self.spinner startAnimation:nil];
    [self runCommand:@"start" completion:^(int status, NSString *text) {
        self.starting = NO;
        [self.spinner stopAnimation:nil];
        self.spinner.hidden = YES;
        if (status != 0) {
            if (self.terminateRequested) { [NSApp replyToApplicationShouldTerminate:YES]; return; }
            [self fail:[@"Не удалось запустить приложение на этом Mac.\n" stringByAppendingString:text]];
            self.retryButton.hidden = NO;
            return;
        }
        self.ready = YES;
        self.openButton.enabled = YES;
        self.status.stringValue = @"Приложение работает. Управляйте загрузками в браузере. Закрытие этого окна не останавливает работу.";
        if (self.terminateRequested) [self stopAndTerminate];
        else if (!self.noBrowser) [self openBrowser:nil];
    }];
}

- (void)openBrowser:(id)sender {
    if (self.ready && !self.stopping) {
        NSURL *url = [NSURL URLWithString:[NSString stringWithFormat:@"http://127.0.0.1:%lu", (unsigned long)self.port]];
        if (![NSWorkspace.sharedWorkspace openURL:url]) [self fail:[NSString stringWithFormat:@"Браузер не открылся. Откройте адрес %@ вручную.", url.absoluteString]];
    }
}

- (void)fail:(NSString *)text {
    self.status.stringValue = @"Приложение не готово. Проверьте сообщение и повторите запуск.";
    NSAlert *alert = [[NSAlert alloc] init];
    alert.messageText = @"YaDiskDownloader";
    alert.informativeText = text;
    [alert addButtonWithTitle:@"Понятно"];
    [alert runModal];
}

- (BOOL)portIsOpen {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return YES;
    struct sockaddr_in address = {0};
    address.sin_family = AF_INET;
    address.sin_port = htons((uint16_t)self.port);
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    BOOL connected = connect(fd, (struct sockaddr *)&address, sizeof(address)) == 0;
    close(fd);
    return connected;
}

- (void)stopAndTerminate {
    self.stopping = YES;
    self.openButton.enabled = NO;
    self.status.stringValue = @"Сохраняем прогресс и завершаем работу…";
    self.spinner.hidden = NO;
    [self.spinner startAnimation:nil];
    [self.window makeKeyAndOrderFront:nil];
    [self runCommand:@"stop" completion:^(int status, NSString *text) {
        dispatch_async(dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
            NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:35];
            while ([self portIsOpen] && deadline.timeIntervalSinceNow > 0) [NSThread sleepForTimeInterval:0.2];
            BOOL stopped = ![self portIsOpen];
            dispatch_async(dispatch_get_main_queue(), ^{
                if (stopped) { [NSApp replyToApplicationShouldTerminate:YES]; return; }
                self.stopping = NO;
                self.terminateRequested = NO;
                self.openButton.enabled = YES;
                [self.spinner stopAnimation:nil];
                self.spinner.hidden = YES;
                [NSApp replyToApplicationShouldTerminate:NO];
                [self fail:status != 0 ? text : @"Приложение на этом Mac ещё сохраняет прогресс. Подождите и повторите «Выход». Принудительное закрытие не выполнялось."];
            });
        });
    }];
}

- (NSApplicationTerminateReply)applicationShouldTerminate:(NSApplication *)sender {
    if (!self.starting && !self.ready) return NSTerminateNow;
    if (self.terminateRequested) return NSTerminateLater;
    self.terminateRequested = YES;
    if (!self.starting) [self stopAndTerminate];
    return NSTerminateLater;
}
- (BOOL)applicationShouldHandleReopen:(NSApplication *)sender hasVisibleWindows:(BOOL)visible {
    [self.window makeKeyAndOrderFront:nil];
    return YES;
}
- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)sender { return NO; }
@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSApplication *app = NSApplication.sharedApplication;
        app.activationPolicy = NSApplicationActivationPolicyRegular;
        DiskLauncher *delegate = [[DiskLauncher alloc] init];
        app.delegate = delegate;
        [app run];
    }
    return 0;
}
