function [movieInfo, exceptions, localMaxima, background, psfSigma] = detectSubResFeatures2D_StandAlone(movieParam, detectionParam, saveResults, verbose)
% Test stub: checks the frame files exist where u-track would read them.
for t = movieParam.firstImageNum:movieParam.lastImageNum
    f = [movieParam.imageDir movieParam.filenameBase sprintf(['%0' num2str(movieParam.digits4Enum) 'd'], t) '.tif'];
    assert(exist(f, 'file') == 2, ['missing frame ' f]);
    img = double(imread(f));
    [~, idx] = max(img(:));
    [y, x] = ind2sub(size(img), idx);
    movieInfo(t, 1).xCoord = [x 0.1; x + 5 0.1];
    movieInfo(t, 1).yCoord = [y 0.1; y + 5 0.1];
    movieInfo(t, 1).amp = [img(idx) 1; 10 1];
end
assert(isstruct(detectionParam.testAlpha));
exceptions = []; localMaxima = []; background = []; psfSigma = detectionParam.psfSigma;
save([saveResults.dir filesep saveResults.filename], 'movieParam', 'detectionParam', 'movieInfo');
end
